from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from typing import Literal

from src.core.models import AttributionTier, DimensionRisk, ReviewState, RiskLevel, WorkItem


@dataclass(frozen=True, slots=True)
class HealthSummary:
    overall_risk: RiskLevel
    high_count: int
    medium_count: int
    low_count: int
    done_count: int
    total_count: int
    delta_direction: Literal["improved", "degraded", "unchanged"]
    prior_counts: dict[str, int] | None
    trajectory: Literal["improving", "stable", "degrading"] = "stable"
    bluf: str | None = None
    leadership_ask: str = "Leadership ask: None this week."
    risk_load: float = 0.0
    prior_risk_load: float | None = None
    risk_load_bar_width: int = 0
    healthy_streak: int = 0
    read_time_minutes: int = 1
    edition_label: str = "Detailed Edition"
    health_reason: str | None = None
    forecast_summary: str | None = None
    forecast_confidence: str | None = None
    status_note: str | None = None
    milestone_summary: str | None = None
    risk_register_summary: str | None = None
    telemetry_summary: str | None = None
    telemetry_confidence: str | None = None
    risk_register_truth_level: str | None = None
    risk_register_disputed: bool = False
    risk_register_stale_evidence: bool = False
    risk_register_includes_unconfirmed_sources: bool = False
    risk_render_warning: str | None = None


@dataclass(frozen=True, slots=True)
class MilestoneSummaryRow:
    name: str
    status: str
    target_date_label: str
    detail: str
    source_document_key: str | None = None
    approval_event_id: str | None = None
    evidence_truth_level: str | None = None
    evidence_disputed: bool = False
    evidence_stale: bool = False


@dataclass(frozen=True, slots=True)
class AssumptionLifecycleRow:
    title: str
    detail: str
    evidence_truth_level: str | None = None
    evidence_disputed: bool = False
    evidence_stale: bool = False


@dataclass(frozen=True, slots=True)
class AssumptionLifecycleSummary:
    window_start: date
    window_end: date
    identified_count: int
    confirmed_count: int
    invalidated_count: int
    still_open_count: int
    rows: tuple[AssumptionLifecycleRow, ...]
    includes_unconfirmed_sources: bool = False
    render_warning: str | None = None


@dataclass(frozen=True, slots=True)
class CharterReviewRow:
    title: str
    detail: str
    status: str | None = None
    evidence: str | None = None


@dataclass(frozen=True, slots=True)
class CharterReviewSummary:
    scope_statement: str | None
    success_criteria_count: int
    constraint_count: int
    rows: tuple[CharterReviewRow, ...]
    evaluated_success_criteria_count: int = 0
    met_success_criteria_count: int = 0
    not_met_success_criteria_count: int = 0
    manual_review_success_criteria_count: int = 0


@dataclass(frozen=True, slots=True)
class RetrospectiveIntelligenceRow:
    category: str
    title: str
    detail: str


@dataclass(frozen=True, slots=True)
class RetrospectiveIntelligenceSummary:
    chronic_workstream_count: int
    recovered_workstream_count: int
    recurring_drift_count: int
    worsened_workstream_count: int
    improved_workstream_count: int
    claim_accuracy_signal_count: int
    charter_evaluation_signal_count: int
    rows: tuple[RetrospectiveIntelligenceRow, ...]


@dataclass(frozen=True, slots=True)
class IncidentLearningRow:
    title: str
    detail: str
    attributed: bool = False


@dataclass(frozen=True, slots=True)
class IncidentLearningSummary:
    window_start: date
    window_end: date
    incident_count: int
    attributed_incident_count: int
    rows: tuple[IncidentLearningRow, ...]
    attributed_patterns: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Top3Item:
    item_type: str
    text: str
    owner: str
    ado_link: str
    anchor: str
    by_date: date | None = None
    label: str | None = None
    suggested: bool = False


@dataclass(frozen=True, slots=True)
class Citation:
    work_item_id: int | None
    title: str
    ado_url: str
    tier: AttributionTier
    label: str | None = None

    @property
    def display_label(self) -> str:
        if self.label:
            return self.label
        if self.work_item_id is None:
            return self.title
        return f"ADO#{self.work_item_id}"


@dataclass(frozen=True, slots=True)
class WorkstreamData:
    section_id: str
    title: str
    blurb: str
    dependency_cascades: tuple[str, ...]
    items: tuple[WorkItem, ...]
    citations: tuple[Citation, ...]
    review_state: ReviewState
    scorecard_name: str | None = None
    risk: RiskLevel | None = None
    prior_risk: RiskLevel | None = None
    derived_risk: RiskLevel | None = None
    override_risk: RiskLevel | None = None
    eta_label: str | None = None
    summary: str = ""
    ado_query_url: str | None = None
    total_items: int = 0
    blocked_count: int = 0
    overdue_count: int = 0
    unowned_count: int = 0
    edit_path: str | None = None
    edit_line: int | None = None
    narrative_empty: bool = False
    significant_findings: tuple[str, ...] = ()
    kpi_tiles: tuple[KpiTile, ...] = ()
    note: str | None = None
    attached_charts: tuple[KustoSectionData, ...] = ()
    source_footnote: str | None = None


@dataclass(frozen=True, slots=True)
class KpiTile:
    query_id: str
    label: str
    value: str
    unit: str | None
    trend: str | None
    confidence: str
    as_of: datetime | None
    source_signal_id: str | None
    render_mode: str = "metric_highlight"
    validated: bool = True
    refresh_on_gather: bool = False
    owner_alias: str | None = None
    reference_url: str | None = None
    catalog_source: dict[str, str] | None = None
    result_payload: dict[str, Any] | None = None
    shared: bool = False


@dataclass(frozen=True, slots=True)
class KustoMetric:
    label: str
    value: str


@dataclass(frozen=True, slots=True)
class KustoTableCell:
    text: str
    href: str | None = None


@dataclass(frozen=True, slots=True)
class KustoSectionData:
    section_id: str
    title: str
    query_id: str
    render_mode: Literal["table", "metric_highlight", "chart_image", "chart"]
    source_label: str
    confidence: str
    columns: tuple[str, ...]
    rows: tuple[tuple[KustoTableCell, ...], ...]
    metrics: tuple[KustoMetric, ...]
    image_data_url: str | None
    reference_url: str | None
    caveats: tuple[str, ...]
    message: str | None
    is_degraded: bool
    # Chart pipeline fields (R3)
    captured_at: datetime | None = None
    chart_png_base64: str | None = None
    chart_png_size_bytes: int = 0
    chart_blocks_publish: bool = False
    chart_cache_ttl_hours: int = 26
    section_placement: str = "standalone"
    cache_captured_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class AdoVitalityAccountabilityRow:
    workstream: str
    owners_and_assignee: str
    ado_label: str
    ado_title: str
    fields_to_update: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AdoVitalitySectionData:
    section_id: str
    title: str
    items_updated: int
    items_total: int
    updated_percentage: int
    freshness_average_days: float
    leakage_events: int
    best_documented_label: str | None
    best_documented_detail: str | None
    trend_summary: str
    accountability_rows: tuple[AdoVitalityAccountabilityRow, ...] = ()


@dataclass(frozen=True, slots=True)
class ScorecardData:
    scorecard_name: str
    dimensions: tuple[DimensionRisk, ...]
    footnote: str | None = None


@dataclass(frozen=True, slots=True)
class DeltaReport:
    new_high_risk: tuple[WorkItem, ...]
    closed_items: tuple[WorkItem, ...]
    risk_up: tuple[WorkItem, ...]
    risk_down: tuple[WorkItem, ...]
    eta_changed: tuple[WorkItem, ...]
    owner_changed: tuple[WorkItem, ...]


@dataclass(frozen=True, slots=True)
class EditionMeta:
    edition: str
    issue_number: int
    generated_at: datetime
    ado_data_as_of: datetime
    manifest_id: str
    qg_status: str
    email_subject: str = ""
    email_preheader: str = ""
    subject_signal: str = ""
    show_orientation: bool = False


@dataclass(frozen=True, slots=True)
class ContinuityJumpLink:
    label: str
    anchor_id: str


@dataclass(frozen=True, slots=True)
class ContinuityBandCell:
    dimension_id: str
    label: str
    risk: RiskLevel
    anchor_id: str | None = None
    query_url: str | None = None
    eta_label: str | None = None


@dataclass(frozen=True, slots=True)
class ContinuityBandData:
    band_id: str
    title: str
    anchor_id: str
    cells: tuple[ContinuityBandCell, ...]


@dataclass(frozen=True, slots=True)
class ContinuityChapterRowData:
    row_number: int
    label: str
    risk: RiskLevel
    owner: str | None
    team: str | None
    state_label: str | None
    eta_label: str | None
    issue_text: str
    approach_text: str | None
    ado_work_item_id: int | None = None
    ado_url: str | None = None


@dataclass(frozen=True, slots=True)
class ContinuityChapterData:
    chapter_id: str
    anchor_id: str
    title: str
    note: str
    chapter_owner: str | None
    rows: tuple[ContinuityChapterRowData, ...]
    review_state: ReviewState


@dataclass(frozen=True, slots=True)
class ContinuityRenderData:
    brand_name: str | None
    brand_header_url: str | None
    edition_intro: str | None
    cadence_note: str
    scorecard_bands: tuple[ContinuityBandData, ...]
    jump_links: tuple[ContinuityJumpLink, ...]
    chapters: tuple[ContinuityChapterData, ...]
