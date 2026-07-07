# Adapted from Shiproom src/report/generator.py
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any
import warnings

from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateNotFound, select_autoescape

from src.core.exceptions import RenderError
from src.core.forecast_engine import ETAForecast
from src.core.jinja_filters import JINJA_FILTERS, JINJA_GLOBALS, build_anchor, delta_label, risk_label
from src.core.models import DeltaKind, DeltaSet, EditionType, FreshnessReport, ItemDelta, ReportData, RunManifest, RiskLevel, ScorecardDelta
from src.core.models import ScorecardEvidencePacket, WorkItem
from src.core.template_contract_loader import TemplateFamilyContract
from src.core.view_models import AdoVitalitySectionData, AssumptionLifecycleSummary, CharterReviewSummary, Citation, ContinuityRenderData, EditionMeta, HealthSummary, IncidentLearningSummary, KustoSectionData, MilestoneSummaryRow, RetrospectiveIntelligenceSummary, ScorecardData, Top3Item, WorkstreamData


REPO_ROOT = Path(__file__).resolve().parents[2]
REPORTS_ROOT = REPO_ROOT / "reports"
TEMPLATES_ROOT = REPO_ROOT / "templates"
_MAX_DELTA_ROW_POOL = 10
_DEFAULT_VISIBLE_CHANGE_ROWS = 5
_CONDENSED_VISIBLE_CHANGE_ROWS = 3
_FLEET_PARITY_TITLE = "Fleet Parity"


@dataclass(frozen=True, slots=True)
class SectionLink:
    label: str
    anchor_id: str


@dataclass(frozen=True, slots=True)
class RenderSection:
    kind: str
    anchor_id: str
    label: str | None
    scorecard: ScorecardData | None = None
    kusto_section: KustoSectionData | None = None
    kusto_group_sections: tuple[KustoSectionData, ...] = ()
    ado_vitality: AdoVitalitySectionData | None = None
    workstream: WorkstreamData | None = None
    charter_review: CharterReviewSummary | None = None
    assumption_lifecycle: AssumptionLifecycleSummary | None = None
    retrospective_intelligence: RetrospectiveIntelligenceSummary | None = None
    incident_learning: IncidentLearningSummary | None = None


@dataclass(frozen=True, slots=True)
class RenderContext:
    title: str
    subtitle: str
    preheader: str
    report: ReportData
    edition_meta: EditionMeta
    layout_mode: str = "dashboard"
    header_label: str | None = None
    footer_label: str | None = None
    health: HealthSummary | None = None
    milestone_rows: tuple[MilestoneSummaryRow, ...] = ()
    top_items: tuple[Top3Item, ...] = ()
    auto_suggestions: tuple[Top3Item, ...] = ()
    forwarding_context: str | None = None
    decision_strip_ack_required: bool = False
    scorecards: tuple[ScorecardData, ...] = ()
    kusto_sections: tuple[KustoSectionData, ...] = ()
    ado_vitality: AdoVitalitySectionData | None = None
    workstreams: tuple[WorkstreamData, ...] = ()
    charter_review: CharterReviewSummary | None = None
    assumption_lifecycle: AssumptionLifecycleSummary | None = None
    retrospective_intelligence: RetrospectiveIntelligenceSummary | None = None
    incident_learning: IncidentLearningSummary | None = None
    exec_summary_citations: tuple[Citation, ...] = ()
    manifest: RunManifest | None = None
    sections: tuple[SectionLink, ...] = ()
    template_contract: TemplateFamilyContract | None = None
    prior_date_label: str | None = None
    changes_url: str | None = None
    item_urls: dict[int, str] = field(default_factory=dict)
    scorecard_packets: dict[str, dict[str, ScorecardEvidencePacket]] = field(default_factory=dict)
    scorecard_deltas: dict[str, dict[str, ScorecardDelta]] = field(default_factory=dict)
    scorecard_urls: dict[str, str] = field(default_factory=dict)
    workstream_urls: dict[str, str] = field(default_factory=dict)
    eta_forecasts: dict[int, ETAForecast] = field(default_factory=dict)
    is_dry_run: bool = False
    workspace_root: str | None = None
    mobile_safe_scorecards: str | None = None
    type_scale_v2: bool = False
    continuity: ContinuityRenderData | None = None
    exec_summary_chart: KustoSectionData | None = None
    show_footer: bool = True
    hidden_render_sections: frozenset[str] = field(default_factory=frozenset)


def build_render_payload(context: RenderContext) -> dict[str, Any]:
    delta_rows = _build_delta_rows(
        deltas=context.report.deltas,
        items=context.report.items,
        item_urls=context.item_urls,
        top_items=context.top_items,
        freshness=context.report.freshness,
        scorecards=context.scorecards,
        scorecard_packets=context.scorecard_packets,
    )
    visible_delta_rows = _limit_visible_delta_rows(delta_rows, context.report.edition)
    workstreams = tuple(ws for ws in context.workstreams if ws.blurb.strip() or ws.items)
    visible_detail_section_ids = {workstream.section_id for workstream in workstreams}
    focused_unchanged_dimension_counts = {
        scorecard.scorecard_name: sum(
            1
            for dimension in scorecard.dimensions
            if build_anchor(f"{scorecard.scorecard_name}-{dimension.name}") not in visible_detail_section_ids
        )
        for scorecard in context.scorecards
    }
    ordered_sections = _build_ordered_sections(context, delta_rows, workstreams)
    if context.sections:
        sections = context.sections
    elif context.template_contract is None:
        sections = _build_default_sections(context)
    else:
        sections = tuple(
            SectionLink(label=section.label, anchor_id=section.anchor_id)
            for section in ordered_sections
            if section.label is not None
        )
    scorecard_totals = {
        scorecard_name: sum(packet.total_items for packet in packets.values())
        for scorecard_name, packets in context.scorecard_packets.items()
    }
    return {
        "title": context.title,
        "subtitle": context.subtitle,
        "preheader": context.preheader,
        "layout_mode": context.layout_mode,
        "header_label": context.header_label,
        "footer_label": context.footer_label,
        "edition": context.edition_meta,
        "health": context.health,
        "milestone_rows": context.milestone_rows,
        "top_items": context.top_items,
        "auto_suggestions": context.auto_suggestions,
        "forwarding_context": context.forwarding_context,
        "decision_strip_ack_required": context.decision_strip_ack_required,
        "report": context.report,
        "scorecards": context.scorecards,
        "kusto_sections": context.kusto_sections,
        "ado_vitality": context.ado_vitality,
        "scorecard_packets": context.scorecard_packets,
        "scorecard_deltas": context.scorecard_deltas,
        "scorecard_totals": scorecard_totals,
        "scorecard_urls": context.scorecard_urls,
        "workstreams": workstreams,
        "visible_detail_section_ids": visible_detail_section_ids,
        "focused_unchanged_dimension_counts": focused_unchanged_dimension_counts,
        "workstream_urls": context.workstream_urls,
        "eta_forecasts": context.eta_forecasts,
        "is_dry_run": context.is_dry_run,
        "workspace_root": context.workspace_root,
        "mobile_safe_scorecards": context.mobile_safe_scorecards,
        "type_scale_v2": context.type_scale_v2,
        "continuity": context.continuity,
        "show_footer": context.show_footer,
        "hidden_render_sections": context.hidden_render_sections,
        "exec_summary_citations": context.exec_summary_citations,
        "exec_summary_chart": context.exec_summary_chart,
        "manifest": context.manifest,
        "sections": sections,
        "ordered_sections": ordered_sections,
        "prior_date": context.prior_date_label or "baseline",
        "delta_rows": delta_rows,
        "visible_delta_rows": visible_delta_rows,
        "delta_counts": {
            "new": len(context.report.deltas.new_items),
            "closed": len(context.report.deltas.closed_items),
            "risk_up": sum(1 for delta in context.report.deltas.risk_changes if delta.kind == DeltaKind.RISK_UP),
            "risk_down": sum(1 for delta in context.report.deltas.risk_changes if delta.kind == DeltaKind.RISK_DOWN),
            "eta": len(context.report.deltas.eta_changes),
        },
        "changes_url": context.changes_url,
        "item_urls": context.item_urls,
    }


class HTMLRenderer:
    def __init__(
        self,
        edition_name: str,
        reports_root: Path = REPORTS_ROOT,
        templates_root: Path = TEMPLATES_ROOT,
    ) -> None:
        search_paths = [str(reports_root / edition_name / "templates"), str(templates_root)]
        self.environment = Environment(
            loader=FileSystemLoader(search_paths),
            autoescape=select_autoescape(["html", "xml"]),
            trim_blocks=True,
            lstrip_blocks=True,
            undefined=StrictUndefined,
        )
        self.environment.filters.update(JINJA_FILTERS)
        self.environment.globals.update(JINJA_GLOBALS)

    def render(self, context: RenderContext) -> str:
        template_name = _select_template(context.report.edition, context.layout_mode)
        payload = build_render_payload(context)
        try:
            return self._get_template(template_name).render(**payload)
        except TemplateNotFound as exc:
            raise RenderError(f"Missing template: {template_name}") from exc

    def render_fragment(self, template_name: str, **context: Any) -> str:
        try:
            return self._get_template(template_name).render(**context)
        except TemplateNotFound as exc:
            raise RenderError(f"Missing template: {template_name}") from exc

    def _get_template(self, template_name: str):
        _warn_if_deprecated_template(template_name)
        return self.environment.get_template(template_name)


def _warn_if_deprecated_template(template_name: str) -> None:
    normalized_name = template_name.replace("\\", "/")
    if normalized_name.endswith("digest.j2"):
        warnings.warn(
            "digest.j2 is deprecated and will be removed after 2026-12-31; use condensed.j2 instead.",
            DeprecationWarning,
            stacklevel=3,
        )


def _select_template(edition_type: EditionType, layout_mode: str = "dashboard") -> str:
    if edition_type == EditionType.LOOKBACK:
        return "archetypes/lookback.j2"
    if layout_mode == "continuity" and edition_type in {EditionType.DETAILED, EditionType.FOCUSED}:
        return "archetypes/continuity.j2"
    if edition_type == EditionType.NARRATIVE:
        return "archetypes/narrative.j2"
    if edition_type == EditionType.CONDENSED:
        return "archetypes/condensed.j2"
    if edition_type in {EditionType.DETAILED, EditionType.FOCUSED}:
        return "archetypes/detailed.j2"
    raise RenderError(f"Edition type {edition_type.value!r} is not supported by the Phase 1A HTML renderer.")


def _build_default_sections(context: RenderContext) -> tuple[SectionLink, ...]:
    sections: list[SectionLink] = []
    grouped_kusto_sections, ungrouped_kusto_sections = _partition_render_kusto_sections(context.kusto_sections)
    if context.report.edition == EditionType.NARRATIVE:
        sections.append(SectionLink(label="Health", anchor_id="health"))
        if context.top_items:
            sections.append(SectionLink(label="Top 3", anchor_id="top-3"))
        for section_id, title, _ in grouped_kusto_sections:
            sections.append(SectionLink(label=_scorecard_nav_label(title), anchor_id=section_id.removeprefix("kusto-group:")))
        for kusto_section in ungrouped_kusto_sections:
            sections.append(SectionLink(label=_scorecard_nav_label(kusto_section.title), anchor_id=kusto_section.section_id))
        for workstream in context.workstreams:
            sections.append(SectionLink(label=_scorecard_nav_label(workstream.title), anchor_id=workstream.section_id))
        if context.report.exec_summary_text.strip():
            sections.append(SectionLink(label="Summary", anchor_id="exec-summary"))
        if context.charter_review is not None:
            sections.append(SectionLink(label="Charter", anchor_id="charter-review"))
        if context.assumption_lifecycle is not None:
            sections.append(SectionLink(label="Assumptions", anchor_id="assumption-lifecycle"))
        if context.incident_learning is not None:
            sections.append(SectionLink(label="Incidents", anchor_id="incident-learnings"))
        if context.retrospective_intelligence is not None:
            sections.append(SectionLink(label="Insights", anchor_id="retrospective-intelligence"))
        return tuple(sections)
    if context.report.exec_summary_text.strip():
        sections.append(SectionLink(label="Summary", anchor_id="exec-summary"))
    if context.incident_learning is not None:
        sections.append(SectionLink(label="Incidents", anchor_id="incident-learnings"))
    if context.retrospective_intelligence is not None:
        sections.append(SectionLink(label="Insights", anchor_id="retrospective-intelligence"))
    if context.charter_review is not None:
        sections.append(SectionLink(label="Charter", anchor_id="charter-review"))
    if context.assumption_lifecycle is not None:
        sections.append(SectionLink(label="Assumptions", anchor_id="assumption-lifecycle"))
    for scorecard in context.scorecards:
        sections.append(SectionLink(label=_scorecard_nav_label(scorecard.scorecard_name), anchor_id=build_anchor(scorecard.scorecard_name)))
    if context.ado_vitality is not None:
        sections.append(SectionLink(label="ADO Vitality", anchor_id=context.ado_vitality.section_id))
    if context.workstreams:
        sections.append(SectionLink(label="Details", anchor_id=context.workstreams[0].section_id))
    if _has_delta_rows(context.report.deltas):
        sections.append(SectionLink(label="Changes", anchor_id="changes"))
    return tuple(sections)


def _build_ordered_sections(
    context: RenderContext,
    delta_rows: tuple[dict[str, str | None], ...],
    workstreams: tuple[WorkstreamData, ...],
) -> tuple[RenderSection, ...]:
    registry = _build_section_registry(context, workstreams)
    ordered_ids = _resolve_section_order(context, workstreams, delta_rows)
    ordered_sections: list[RenderSection] = []
    seen: set[str] = set()
    for section_id in ordered_ids:
        section = registry.get(section_id)
        if section is None:
            continue
        if section.anchor_id in seen:
            continue
        if not _section_enabled(section_id, context):
            continue
        ordered_sections.append(section)
        seen.add(section.anchor_id)
    return tuple(ordered_sections)


def _build_section_registry(
    context: RenderContext,
    workstreams: tuple[WorkstreamData, ...],
) -> dict[str, RenderSection]:
    grouped_kusto_sections, ungrouped_kusto_sections = _partition_render_kusto_sections(context.kusto_sections)
    registry = {
        "health": RenderSection(kind="health", anchor_id="health", label="Health"),
        "provenance": RenderSection(kind="provenance", anchor_id="provenance", label=None),
    }
    registry["top_3"] = RenderSection(kind="top_3", anchor_id="top-3", label="Decisions")
    if context.report.exec_summary_text.strip():
        registry["exec_summary"] = RenderSection(kind="exec_summary", anchor_id="exec-summary", label="Summary")
    if context.charter_review is not None:
        registry["charter_review"] = RenderSection(
            kind="charter_review",
            anchor_id="charter-review",
            label="Charter",
            charter_review=context.charter_review,
        )
    if context.assumption_lifecycle is not None:
        registry["assumption_lifecycle"] = RenderSection(
            kind="assumption_lifecycle",
            anchor_id="assumption-lifecycle",
            label="Assumptions",
            assumption_lifecycle=context.assumption_lifecycle,
        )
    if context.incident_learning is not None:
        registry["incident_learning"] = RenderSection(
            kind="incident_learning",
            anchor_id="incident-learnings",
            label="Incidents",
            incident_learning=context.incident_learning,
        )
    if context.retrospective_intelligence is not None:
        registry["retrospective_intelligence"] = RenderSection(
            kind="retrospective_intelligence",
            anchor_id="retrospective-intelligence",
            label="Insights",
            retrospective_intelligence=context.retrospective_intelligence,
        )
    if _has_delta_rows(context.report.deltas):
        registry["selected_changes"] = RenderSection(kind="selected_changes", anchor_id="changes", label="Changes")
    for scorecard in context.scorecards:
        scorecard_anchor = build_anchor(scorecard.scorecard_name)
        registry[f"scorecard:{scorecard_anchor}"] = RenderSection(
            kind="scorecard",
            anchor_id=scorecard_anchor,
            label=_scorecard_nav_label(scorecard.scorecard_name),
            scorecard=scorecard,
        )
    for section_id, title, grouped_sections in grouped_kusto_sections:
        registry[section_id] = RenderSection(
            kind="kusto_group",
            anchor_id=section_id.removeprefix("kusto-group:"),
            label=_scorecard_nav_label(title),
            kusto_group_sections=grouped_sections,
        )
    for kusto_section in ungrouped_kusto_sections:
        registry[f"kusto:{kusto_section.section_id}"] = RenderSection(
            kind="kusto",
            anchor_id=kusto_section.section_id,
            label=_scorecard_nav_label(kusto_section.title),
            kusto_section=kusto_section,
        )
    if context.ado_vitality is not None:
        registry["ado_vitality"] = RenderSection(
            kind="ado_vitality",
            anchor_id=context.ado_vitality.section_id,
            label="ADO Vitality",
            ado_vitality=context.ado_vitality,
        )
    for workstream in workstreams:
        registry[f"workstream:{workstream.section_id}"] = RenderSection(
            kind="workstream",
            anchor_id=workstream.section_id,
            label=_scorecard_nav_label(workstream.title),
            workstream=workstream,
        )
    return registry


def _resolve_section_order(
    context: RenderContext,
    workstreams: tuple[WorkstreamData, ...],
    delta_rows: tuple[dict[str, str | None], ...],
) -> tuple[str, ...]:
    grouped_kusto_sections, ungrouped_kusto_sections = _partition_render_kusto_sections(context.kusto_sections)
    if context.template_contract is None:
        return _default_section_order(context, workstreams, delta_rows)

    ordered_ids: list[str] = []
    for entry in context.template_contract.order:
        if entry == "scorecards:all":
            ordered_ids.extend(f"scorecard:{build_anchor(scorecard.scorecard_name)}" for scorecard in context.scorecards)
            continue
        if entry == "kusto:all":
            ordered_ids.extend(section_id for section_id, _, _ in grouped_kusto_sections)
            ordered_ids.extend(f"kusto:{kusto_section.section_id}" for kusto_section in ungrouped_kusto_sections)
            continue
        if entry == "workstreams:all":
            # Two-pass ordering: emit Blocked/High/Medium grouped by scorecard first,
            # then Low/Done/Unknown grouped by scorecard at the end.
            _RISK_SORT_ORDER = {"high": 0, "medium": 1, "low": 2, "done": 3, "unknown": 4}
            _LOW_THRESHOLD = 2  # risk ranks >= this are "low priority" (low, done, unknown)
            scorecard_order = [scorecard.scorecard_name for scorecard in context.scorecards]
            grouped: dict[str, list[tuple[int, str]]] = {name: [] for name in scorecard_order}
            ungrouped: list[tuple[int, str]] = []
            for workstream in workstreams:
                ws_id = f"workstream:{workstream.section_id}"
                risk_rank = _RISK_SORT_ORDER.get(workstream.risk.value if workstream.risk else "unknown", 4)
                if workstream.scorecard_name in grouped:
                    grouped[workstream.scorecard_name].append((risk_rank, ws_id))
                else:
                    ungrouped.append((risk_rank, ws_id))
            # First pass: non-Low items (Blocked/High/Medium) per scorecard
            for sc_name in scorecard_order:
                high_items = [(r, wid) for r, wid in grouped[sc_name] if r < _LOW_THRESHOLD]
                high_items.sort(key=lambda x: x[0])
                ordered_ids.extend(ws_id for _, ws_id in high_items)
            high_ungrouped = [(r, wid) for r, wid in ungrouped if r < _LOW_THRESHOLD]
            high_ungrouped.sort(key=lambda x: x[0])
            ordered_ids.extend(ws_id for _, ws_id in high_ungrouped)
            # Second pass: Low/Done/Unknown items per scorecard
            for sc_name in scorecard_order:
                low_items = [(r, wid) for r, wid in grouped[sc_name] if r >= _LOW_THRESHOLD]
                low_items.sort(key=lambda x: x[0])
                ordered_ids.extend(ws_id for _, ws_id in low_items)
            low_ungrouped = [(r, wid) for r, wid in ungrouped if r >= _LOW_THRESHOLD]
            low_ungrouped.sort(key=lambda x: x[0])
            ordered_ids.extend(ws_id for _, ws_id in low_ungrouped)
            continue
        ordered_ids.append(entry)
    return tuple(ordered_ids)


def _default_section_order(
    context: RenderContext,
    workstreams: tuple[WorkstreamData, ...],
    delta_rows: tuple[dict[str, str | None], ...],
) -> tuple[str, ...]:
    grouped_kusto_sections, ungrouped_kusto_sections = _partition_render_kusto_sections(context.kusto_sections)
    ordered_ids: list[str] = []
    if context.report.edition == EditionType.NARRATIVE:
        ordered_ids.append("health")
        ordered_ids.append("top_3")
        if context.report.exec_summary_text.strip():
            ordered_ids.append("exec_summary")
        ordered_ids.extend(section_id for section_id, _, _ in grouped_kusto_sections)
        ordered_ids.extend(f"kusto:{kusto_section.section_id}" for kusto_section in ungrouped_kusto_sections)
        ordered_ids.extend(f"workstream:{workstream.section_id}" for workstream in workstreams)
        ordered_ids.append("provenance")
        return tuple(ordered_ids)
    ordered_ids.append("health")
    ordered_ids.append("top_3")
    ordered_ids.extend(f"scorecard:{build_anchor(scorecard.scorecard_name)}" for scorecard in context.scorecards)
    if context.ado_vitality is not None:
        ordered_ids.append("ado_vitality")
    if delta_rows:
        ordered_ids.append("selected_changes")
    if context.report.exec_summary_text.strip():
        ordered_ids.append("exec_summary")
    if context.incident_learning is not None:
        ordered_ids.append("incident_learning")
    if context.retrospective_intelligence is not None:
        ordered_ids.append("retrospective_intelligence")
    if context.charter_review is not None:
        ordered_ids.append("charter_review")
    if context.assumption_lifecycle is not None:
        ordered_ids.append("assumption_lifecycle")
    # Two-pass ordering: emit Blocked/High/Medium grouped by scorecard first,
    # then Low/Done/Unknown grouped by scorecard at the end.
    _RISK_ORDER = {"blocked": 0, "high": 1, "medium": 2, "low": 3, "done": 4, "unknown": 5}
    _LOW_THRESHOLD = 2
    scorecard_order = [scorecard.scorecard_name for scorecard in context.scorecards]
    grouped_default: dict[str, list[tuple[int, str]]] = {name: [] for name in scorecard_order}
    ungrouped_default: list[tuple[int, str]] = []
    for workstream in workstreams:
        ws_id = f"workstream:{workstream.section_id}"
        risk_rank = _RISK_ORDER.get(workstream.risk.value if workstream.risk else "unknown", 4)
        if workstream.scorecard_name in grouped_default:
            grouped_default[workstream.scorecard_name].append((risk_rank, ws_id))
        else:
            ungrouped_default.append((risk_rank, ws_id))
    # First pass: non-Low items (Blocked/High/Medium) per scorecard
    for sc_name in scorecard_order:
        high_items = [(r, wid) for r, wid in grouped_default[sc_name] if r < _LOW_THRESHOLD]
        high_items.sort(key=lambda x: x[0])
        ordered_ids.extend(ws_id for _, ws_id in high_items)
    high_ungrouped = [(r, wid) for r, wid in ungrouped_default if r < _LOW_THRESHOLD]
    high_ungrouped.sort(key=lambda x: x[0])
    ordered_ids.extend(ws_id for _, ws_id in high_ungrouped)
    # Second pass: Low/Done/Unknown items per scorecard
    for sc_name in scorecard_order:
        low_items = [(r, wid) for r, wid in grouped_default[sc_name] if r >= _LOW_THRESHOLD]
        low_items.sort(key=lambda x: x[0])
        ordered_ids.extend(ws_id for _, ws_id in low_items)
    low_ungrouped = [(r, wid) for r, wid in ungrouped_default if r >= _LOW_THRESHOLD]
    low_ungrouped.sort(key=lambda x: x[0])
    ordered_ids.extend(ws_id for _, ws_id in low_ungrouped)
    ordered_ids.extend(section_id for section_id, _, _ in grouped_kusto_sections)
    ordered_ids.extend(f"kusto:{kusto_section.section_id}" for kusto_section in ungrouped_kusto_sections)
    ordered_ids.append("provenance")
    return tuple(ordered_ids)


def _partition_render_kusto_sections(
    kusto_sections: tuple[KustoSectionData, ...],
) -> tuple[tuple[tuple[str, str, tuple[KustoSectionData, ...]], ...], tuple[KustoSectionData, ...]]:
    fleet_parity_sections = tuple(section for section in kusto_sections if section.title == _FLEET_PARITY_TITLE)
    if not fleet_parity_sections:
        return (), kusto_sections
    grouped = (("kusto-group:fleet-parity", _FLEET_PARITY_TITLE, fleet_parity_sections),)
    ungrouped = tuple(section for section in kusto_sections if section.title != _FLEET_PARITY_TITLE)
    return grouped, ungrouped


def _section_enabled(section_id: str, context: RenderContext) -> bool:
    # Check if the section is explicitly hidden via overrides removed_sections.
    bare_id = section_id.split(":", 1)[-1] if ":" in section_id else section_id
    # Normalize: check both hyphen and underscore variants since overrides use hyphens but registry uses underscores.
    bare_id_alt = bare_id.replace("_", "-")
    if bare_id in context.hidden_render_sections or section_id in context.hidden_render_sections or bare_id_alt in context.hidden_render_sections:
        return False
    if context.template_contract is None:
        return True
    rule = context.template_contract.rule_for(section_id)
    if rule is None or rule.render_only_if is None:
        return True
    if rule.render_only_if == "baseline_available":
        return context.report.deltas.previous_issue_number is not None
    return True


def _scorecard_nav_label(scorecard_name: str) -> str:
    return scorecard_name if len(scorecard_name) <= 14 else f"{scorecard_name[:11]}..."


def _has_delta_rows(deltas: DeltaSet) -> bool:
    return any(
        (
            deltas.new_items,
            deltas.closed_items,
            deltas.risk_changes,
            deltas.eta_changes,
            getattr(deltas, "owner_changes", ()),
        )
    )


def _visible_change_row_limit(edition: EditionType) -> int:
    if edition == EditionType.CONDENSED:
        return _CONDENSED_VISIBLE_CHANGE_ROWS
    return _DEFAULT_VISIBLE_CHANGE_ROWS


def _limit_visible_delta_rows(
    delta_rows: tuple[dict[str, str | None], ...],
    edition: EditionType,
) -> tuple[dict[str, str | None], ...]:
    return delta_rows[:_visible_change_row_limit(edition)]


def _build_delta_rows(
    *,
    deltas: DeltaSet,
    items: tuple[WorkItem, ...],
    item_urls: dict[int, str],
    top_items: tuple[Top3Item, ...],
    freshness: FreshnessReport,
    scorecards: tuple[ScorecardData, ...],
    scorecard_packets: dict[str, dict[str, ScorecardEvidencePacket]],
) -> tuple[dict[str, str | None], ...]:
    item_lookup = {item.id: item for item in items}
    membership = _build_scorecard_membership(scorecards, scorecard_packets)
    decision_item_ids = _decision_linked_item_ids(top_items, membership)
    freshness_rules = _freshness_rules_by_item(freshness)
    represented_item_ids: set[int] = set()
    rows: list[tuple[tuple[int, int, int, date, float, int, int], dict[str, str | None]]] = []

    ordered_deltas = [
        *deltas.new_items,
        *deltas.risk_changes,
        *deltas.eta_changes,
        *tuple(getattr(deltas, "owner_changes", ())),
        *deltas.closed_items,
    ]
    for delta in ordered_deltas:
        item = item_lookup.get(delta.work_item_id)
        category = _delta_priority_category(
            delta=delta,
            decision_item_ids=decision_item_ids,
            freshness_rules=freshness_rules,
        )
        represented_item_ids.add(delta.work_item_id)
        rows.append(
            (
                _delta_sort_key(
                    category=category,
                    item=item,
                    delta=delta,
                    section_order=membership.item_section_order.get(delta.work_item_id, 1_000_000),
                ),
                {
                    "kind": delta.kind.value,
                    "label": _delta_category_label(category, delta.kind),
                    "title": item.title if item is not None else f"Work item {delta.work_item_id}",
                    "detail": _delta_row_detail(delta, category=category, freshness_rules=freshness_rules.get(delta.work_item_id, ())),
                    "url": item_urls.get(delta.work_item_id),
                    "work_item_id": str(delta.work_item_id),
                },
            )
        )

    for item_id, rule_ids in freshness_rules.items():
        if item_id in represented_item_ids:
            continue
        item = item_lookup.get(item_id)
        if item is None:
            continue
        represented_item_ids.add(item_id)
        rows.append(
            (
                _delta_sort_key(
                    category="freshness_block",
                    item=item,
                    delta=None,
                    section_order=membership.item_section_order.get(item_id, 1_000_000),
                ),
                {
                    "kind": DeltaKind.ETA_CHANGED.value,
                    "label": "FR Block",
                    "title": item.title,
                    "detail": ", ".join(rule_ids),
                    "url": item_urls.get(item_id),
                    "work_item_id": str(item_id),
                },
            )
        )

    for synthetic_row in _synthetic_dimension_rows(
        membership=membership,
        represented_item_ids=represented_item_ids,
        item_lookup=item_lookup,
        item_urls=item_urls,
    ):
        rows.append(synthetic_row)

    rows.sort(key=lambda entry: entry[0])
    return tuple(row for _, row in rows[:_MAX_DELTA_ROW_POOL])


@dataclass(frozen=True, slots=True)
class _ScorecardMembership:
    item_section_order: dict[int, int]
    scorecard_anchor_items: dict[str, set[int]]
    detail_anchor_items: dict[str, set[int]]
    chronic_candidates: tuple[tuple[str, str, ScorecardEvidencePacket, RiskLevel], ...]
    stale_candidates: tuple[tuple[str, str, ScorecardEvidencePacket, RiskLevel], ...]


def _build_scorecard_membership(
    scorecards: tuple[ScorecardData, ...],
    scorecard_packets: dict[str, dict[str, ScorecardEvidencePacket]],
) -> _ScorecardMembership:
    item_section_order: dict[int, int] = {}
    scorecard_anchor_items: dict[str, set[int]] = {}
    detail_anchor_items: dict[str, set[int]] = {}
    chronic_candidates: list[tuple[str, str, ScorecardEvidencePacket, RiskLevel]] = []
    stale_candidates: list[tuple[str, str, ScorecardEvidencePacket, RiskLevel]] = []
    dimension_risks = {
        (scorecard.scorecard_name, dimension.name): dimension.risk
        for scorecard in scorecards
        for dimension in scorecard.dimensions
    }

    for scorecard_index, scorecard in enumerate(scorecards):
        scorecard_anchor = build_anchor(scorecard.scorecard_name)
        scorecard_items = scorecard_anchor_items.setdefault(scorecard_anchor, set())
        packet_map = scorecard_packets.get(scorecard.scorecard_name, {})
        for dimension_index, dimension in enumerate(scorecard.dimensions):
            packet = packet_map.get(dimension.name)
            if packet is None:
                continue
            item_ids = packet.item_ids or tuple(_parse_item_id_from_url(link) for link in packet.item_links if _parse_item_id_from_url(link) is not None)  # type: ignore[misc]
            detail_anchor = build_anchor(f"{scorecard.scorecard_name}-{dimension.name}")
            detail_items = detail_anchor_items.setdefault(detail_anchor, set())
            for item_id in item_ids:
                scorecard_items.add(item_id)
                detail_items.add(item_id)
                item_section_order.setdefault(item_id, scorecard_index * 100 + dimension_index)
            current_risk = dimension_risks.get((scorecard.scorecard_name, dimension.name), packet.derived_risk)
            if current_risk == RiskLevel.HIGH and packet.streak_count >= 3:
                chronic_candidates.append((scorecard.scorecard_name, dimension.name, packet, current_risk))
            if current_risk in {RiskLevel.BLOCKED, RiskLevel.HIGH, RiskLevel.MEDIUM} and (packet.stale_count > 0 or packet.is_stale_dimension):
                stale_candidates.append((scorecard.scorecard_name, dimension.name, packet, current_risk))

    return _ScorecardMembership(
        item_section_order=item_section_order,
        scorecard_anchor_items=scorecard_anchor_items,
        detail_anchor_items=detail_anchor_items,
        chronic_candidates=tuple(chronic_candidates),
        stale_candidates=tuple(stale_candidates),
    )


def _decision_linked_item_ids(
    top_items: tuple[Top3Item, ...],
    membership: _ScorecardMembership,
) -> set[int]:
    item_ids: set[int] = set()
    for top_item in top_items:
        parsed_item_id = _parse_item_id_from_url(top_item.ado_link)
        if parsed_item_id is not None:
            item_ids.add(parsed_item_id)
        item_ids.update(membership.detail_anchor_items.get(top_item.anchor, set()))
        item_ids.update(membership.scorecard_anchor_items.get(top_item.anchor, set()))
    return item_ids


def _parse_item_id_from_url(url: str | None) -> int | None:
    if not url:
        return None
    normalized = url.strip().rstrip("/")
    for marker in ("/_workitems/edit/", "/workitems/"):
        if marker not in normalized:
            continue
        tail = normalized.rsplit(marker, 1)[-1]
        digits = "".join(character for character in tail if character.isdigit())
        if digits:
            return int(digits)
    return None


def _freshness_rules_by_item(freshness: FreshnessReport) -> dict[int, tuple[str, ...]]:
    grouped: dict[int, list[str]] = {}
    for item in freshness.items:
        if item.rule_id not in {"FR-44", "FR-45", "FR-47"}:
            continue
        grouped.setdefault(item.work_item_id, []).append(item.rule_id)
    return {work_item_id: tuple(sorted(set(rule_ids))) for work_item_id, rule_ids in grouped.items()}


def _synthetic_dimension_rows(
    *,
    membership: _ScorecardMembership,
    represented_item_ids: set[int],
    item_lookup: dict[int, WorkItem],
    item_urls: dict[int, str],
) -> tuple[tuple[tuple[int, int, int, date, float, int, int], dict[str, str | None]], ...]:
    synthetic_rows: list[tuple[tuple[int, int, int, date, float, int, int], dict[str, str | None]]] = []

    for scorecard_name, dimension_name, packet, current_risk in membership.chronic_candidates:
        representative_id = _first_unrepresented_item(packet.item_ids, represented_item_ids)
        if representative_id is None:
            continue
        item = item_lookup.get(representative_id)
        if item is None:
            continue
        represented_item_ids.add(representative_id)
        synthetic_rows.append(
            (
                _delta_sort_key(
                    category="chronic_high",
                    item=item,
                    delta=None,
                    section_order=membership.item_section_order.get(representative_id, 1_000_000),
                ),
                {
                    "kind": DeltaKind.UNCHANGED.value,
                    "label": f"High {packet.streak_count}w",
                    "title": item.title,
                    "detail": f"{dimension_name} remained High for {packet.streak_count} issues.",
                    "url": item_urls.get(representative_id),
                    "work_item_id": str(representative_id),
                },
            )
        )

    for scorecard_name, dimension_name, packet, current_risk in membership.stale_candidates:
        candidate_ids = packet.stale_items or packet.item_ids
        representative_id = _first_unrepresented_item(candidate_ids, represented_item_ids)
        if representative_id is None:
            continue
        item = item_lookup.get(representative_id)
        if item is None:
            continue
        represented_item_ids.add(representative_id)
        synthetic_rows.append(
            (
                _delta_sort_key(
                    category="stale",
                    item=item,
                    delta=None,
                    section_order=membership.item_section_order.get(representative_id, 1_000_000),
                ),
                {
                    "kind": DeltaKind.ETA_CHANGED.value,
                    "label": "Stale",
                    "title": item.title,
                    "detail": f"{dimension_name} has {packet.stale_count} stale High/Medium item(s).",
                    "url": item_urls.get(representative_id),
                    "work_item_id": str(representative_id),
                },
            )
        )

    return tuple(synthetic_rows)


def _first_unrepresented_item(item_ids: tuple[int, ...], represented_item_ids: set[int]) -> int | None:
    for item_id in item_ids:
        if item_id not in represented_item_ids:
            return item_id
    return None


def _delta_priority_category(
    *,
    delta: ItemDelta,
    decision_item_ids: set[int],
    freshness_rules: dict[int, tuple[str, ...]],
) -> str:
    if _is_new_high(delta):
        return "new_high"
    if delta.work_item_id in decision_item_ids:
        return "decision_linked"
    if delta.work_item_id in freshness_rules:
        return "freshness_block"
    if delta.kind == DeltaKind.RISK_UP:
        return "risk_up"
    if _is_material_eta_slip(delta):
        return "eta_slip"
    if delta.kind == DeltaKind.OWNER_CHANGED:
        return "owner_change"
    if delta.kind == DeltaKind.RISK_DOWN:
        return "risk_down"
    if delta.kind == DeltaKind.CLOSED:
        return "closed"
    if delta.kind == DeltaKind.NEW:
        return "new"
    return "other"


def _is_new_high(delta: ItemDelta) -> bool:
    return delta.new_risk in {RiskLevel.BLOCKED, RiskLevel.HIGH} and delta.old_risk not in {RiskLevel.BLOCKED, RiskLevel.HIGH}


def _is_material_eta_slip(delta: ItemDelta, threshold_business_days: int = 5) -> bool:
    if delta.kind != DeltaKind.ETA_CHANGED or delta.old_eta is None or delta.new_eta is None:
        return False
    if delta.new_eta <= delta.old_eta:
        return False
    return _business_days_between(delta.old_eta, delta.new_eta) >= threshold_business_days


def _business_days_between(start: date, end: date) -> int:
    if end <= start:
        return 0
    cursor = start
    business_days = 0
    while cursor < end:
        cursor = date.fromordinal(cursor.toordinal() + 1)
        if cursor.weekday() < 5:
            business_days += 1
    return business_days


def _delta_sort_key(
    *,
    category: str,
    item: WorkItem | None,
    delta: ItemDelta | None,
    section_order: int,
) -> tuple[int, int, int, date, float, int, int]:
    current_risk = _current_risk(item=item, delta=delta)
    eta_sort = item.target_date if item is not None and item.target_date is not None else (delta.new_eta if delta is not None and delta.new_eta is not None else date.max)
    updated_at = _item_last_updated(item)
    return (
        _delta_category_priority(category),
        -_risk_rank(current_risk),
        -_risk_contribution(current_risk),
        eta_sort,
        -updated_at.timestamp(),
        section_order,
        item.id if item is not None else (delta.work_item_id if delta is not None else 0),
    )


def _current_risk(*, item: WorkItem | None, delta: ItemDelta | None) -> RiskLevel:
    if delta is not None and delta.new_risk is not None:
        return delta.new_risk
    if item is not None:
        return item.risk_level
    return RiskLevel.UNKNOWN


def _item_last_updated(item: WorkItem | None) -> datetime:
    if item is None:
        return datetime.min.replace(tzinfo=None)
    timestamps = [revision.changed_date for revision in item.revisions]
    timestamps.extend(comment.created_date for comment in item.comments)
    timestamps.append(item.fetched_at)
    return max(timestamps)


def _delta_category_priority(category: str) -> int:
    priorities = {
        "new_high": 1,
        "decision_linked": 2,
        "freshness_block": 3,
        "risk_up": 4,
        "eta_slip": 5,
        "chronic_high": 6,
        "stale": 7,
        "owner_change": 8,
        "risk_down": 9,
        "closed": 10,
        "new": 11,
        "other": 12,
    }
    return priorities.get(category, 12)


def _risk_rank(level: RiskLevel) -> int:
    ranks = {
        RiskLevel.HIGH: 4,
        RiskLevel.MEDIUM: 3,
        RiskLevel.UNKNOWN: 2,
        RiskLevel.LOW: 1,
        RiskLevel.DONE: 0,
    }
    return ranks[level]


def _risk_contribution(level: RiskLevel) -> int:
    weights = {
        RiskLevel.HIGH: 3,
        RiskLevel.MEDIUM: 1,
        RiskLevel.UNKNOWN: 2,
        RiskLevel.LOW: 0,
        RiskLevel.DONE: 0,
    }
    return weights[level]


def _delta_category_label(category: str, kind: DeltaKind) -> str:
    labels = {
        "new_high": "New High",
        "decision_linked": "Decision",
        "freshness_block": "FR Block",
        "risk_up": "Risk Up",
        "eta_slip": "ETA Slip",
        "chronic_high": "High 3w+",
        "stale": "Stale",
        "owner_change": "Owner",
        "risk_down": "Risk Down",
        "closed": "Closed",
        "new": "New",
    }
    return labels.get(category, _delta_row_label(kind))


def _delta_row_label(kind: DeltaKind) -> str:
    if kind == DeltaKind.RISK_UP:
        return "Risk Up"
    if kind == DeltaKind.RISK_DOWN:
        return "Risk Down"
    if kind == DeltaKind.NEW:
        return "New"
    if kind == DeltaKind.CLOSED:
        return "Closed"
    if kind == DeltaKind.ETA_CHANGED:
        return "ETA Shift"
    if kind == DeltaKind.OWNER_CHANGED:
        return "Owner"
    return "No Change"


def _delta_row_detail(
    delta: ItemDelta,
    *,
    category: str,
    freshness_rules: tuple[str, ...] = (),
) -> str:
    if category == "freshness_block" and freshness_rules:
        return ", ".join(freshness_rules)
    if delta.kind in {DeltaKind.RISK_UP, DeltaKind.RISK_DOWN}:
        return f"{risk_label(delta.old_risk)} → {risk_label(delta.new_risk)}"
    if delta.kind == DeltaKind.NEW:
        return risk_label(delta.new_risk)
    if delta.kind == DeltaKind.CLOSED:
        return "Done"
    if delta.kind == DeltaKind.ETA_CHANGED:
        return delta_label(delta.kind, delta.old_eta, delta.new_eta)
    if delta.kind == DeltaKind.OWNER_CHANGED:
        old_owner, new_owner = delta.field_changes.get("assigned_to", (None, None))
        return f"{old_owner or 'Unassigned'} → {new_owner or 'Unassigned'}"
    return "—"