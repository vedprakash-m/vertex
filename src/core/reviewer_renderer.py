from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateNotFound, select_autoescape

from src.core.exceptions import RenderError
from src.core.html_renderer import REPORTS_ROOT, TEMPLATES_ROOT
from src.core.jinja_filters import JINJA_FILTERS, JINJA_GLOBALS, risk_label
from src.core.models import ReviewState, RiskLevel
from src.core.models_v2 import WorkstreamCalibration


def _review_state_icon(state: ReviewState) -> str:
    if state == ReviewState.APPROVED:
        return "✅"
    if state == ReviewState.SKIPPED_NO_DELTA:
        return "•"
    if state == ReviewState.CHANGES_REQUESTED:
        return "✏️"
    if state == ReviewState.REJECTED:
        return "❌"
    return "⏳"


@dataclass(frozen=True, slots=True)
class ReviewerStatusChip:
    section_id: str
    label: str
    state: ReviewState
    reviewer: str | None
    raci_summary: str | None = None

    @property
    def reviewer_display(self) -> str:
        return self.reviewer or "unassigned"

    @property
    def state_icon(self) -> str:
        return _review_state_icon(self.state)

    @property
    def state_label(self) -> str:
        if self.state == ReviewState.SKIPPED_NO_DELTA:
            return "Skipped"
        return self.state.value.replace("_", " ").title()


@dataclass(frozen=True, slots=True)
class ReviewerDeltaRow:
    label: str
    title: str
    detail: str
    ado_url: str | None


@dataclass(frozen=True, slots=True)
class ReviewerEvidenceRow:
    work_item_id: int
    title: str
    summary: str
    ado_url: str | None


@dataclass(frozen=True, slots=True)
class ReviewerContextRow:
    label: str
    summary: str
    ado_url: str | None


@dataclass(frozen=True, slots=True)
class ReviewerWhyLine:
    label: str
    value: str
    href: str | None = None


@dataclass(frozen=True, slots=True)
class ReviewerWhyBlock:
    summary: str
    confidence: str
    evidence_command: str
    lines: tuple[ReviewerWhyLine, ...]


@dataclass(frozen=True, slots=True)
class ReviewerOverrideRow:
    scorecard_name: str
    dimension_name: str
    current_risk: RiskLevel
    prior_risk: RiskLevel | None
    summary: str
    ado_query_url: str | None

    @property
    def current_risk_label(self) -> str:
        return risk_label(self.current_risk)

    @property
    def prior_risk_label(self) -> str:
        if self.prior_risk is None:
            return "None"
        return risk_label(self.prior_risk)


@dataclass(frozen=True, slots=True)
class ReviewerVitalityRow:
    owner_alias: str
    composite_score: int
    fresh_items: int
    total_items: int
    leakage_events: int

    @property
    def bar_width(self) -> int:
        return max(0, min(self.composite_score, 100))

    @property
    def leakage_label(self) -> str:
        if self.leakage_events == 1:
            return "1 leakage"
        return f"{self.leakage_events} leakage"

    @property
    def summary(self) -> str:
        item_label = "item" if self.total_items == 1 else "items"
        return f"{self.fresh_items}/{self.total_items} {item_label} fresh, {self.leakage_label}"

    @property
    def perfect_score(self) -> bool:
        return self.composite_score >= 100


@dataclass(frozen=True, slots=True)
class ReviewerAnticipatedQuestion:
    reader: str
    question: str
    suggested_response: str
    confidence: str
    evidence: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReviewerTrustRow:
    label: str
    summary: str
    trust_level: str
    confidence_percent: int


@dataclass(frozen=True, slots=True)
class ReviewerAttentionGapRow:
    workstream_id: str
    slip_modifier: float
    attention_weight: float
    bridge_summary: str


@dataclass(frozen=True, slots=True)
class ReviewerInlineLink:
    label: str
    href: str


@dataclass(frozen=True, slots=True)
class ReviewerTrackedEntryRow:
    title: str
    summary: str
    detail: str | None = None
    href: str | None = None
    anchor_id: str | None = None
    links: tuple[ReviewerInlineLink, ...] = ()


@dataclass(frozen=True, slots=True)
class ReviewerMilestoneTimelineRow:
    milestone_id: str
    name: str
    target_date_label: str
    declared_status: str
    computed_status: str
    critical_path: bool = False
    schedule_summary: str | None = None
    target_history_summary: str | None = None
    completion_history_summary: str | None = None

    @property
    def status_summary(self) -> str:
        labels = [f"computed {self.computed_status}", f"declared {self.declared_status}"]
        if self.critical_path:
            labels.append("critical path")
        if self.schedule_summary:
            labels.append(self.schedule_summary)
        if self.target_history_summary:
            labels.append(self.target_history_summary)
        if self.completion_history_summary:
            labels.append(self.completion_history_summary)
        return " · ".join(labels)


@dataclass(frozen=True, slots=True)
class ReviewerSignalThreadEvent:
    timestamp_label: str
    source: str
    confidence: str
    text: str


@dataclass(frozen=True, slots=True)
class ReviewerSignalThreadRow:
    thread_id: str
    signal_count: int
    detail: str
    events: tuple[ReviewerSignalThreadEvent, ...]

    @property
    def title(self) -> str:
        label = "signal" if self.signal_count == 1 else "signals"
        return f"{self.thread_id} · {self.signal_count} {label}"


@dataclass(frozen=True, slots=True)
class ReviewerSimilarityBadge:
    issue_number: int
    generated_at: datetime
    similarity: float
    excerpt: str
    risk_level: RiskLevel | None = None

    @property
    def similarity_percent(self) -> int:
        return round(self.similarity * 100)

    @property
    def generated_at_label(self) -> str:
        return self.generated_at.date().isoformat()

    @property
    def risk_label_text(self) -> str | None:
        if self.risk_level is None:
            return None
        return risk_label(self.risk_level)


@dataclass(frozen=True, slots=True)
class ReviewerSectionData:
    section_id: str
    title: str
    published_text: str
    state: ReviewState
    reviewer: str | None
    note: str | None
    delta_rows: tuple[ReviewerDeltaRow, ...]
    evidence_rows: tuple[ReviewerEvidenceRow, ...]
    override_rows: tuple[ReviewerOverrideRow, ...]
    context_rows: tuple[ReviewerContextRow, ...] = ()
    why_block: ReviewerWhyBlock | None = None
    item_ids: tuple[int, ...] = ()
    similarity_badge: ReviewerSimilarityBadge | None = None

    @property
    def reviewer_display(self) -> str:
        return self.reviewer or "unassigned"

    @property
    def state_icon(self) -> str:
        return _review_state_icon(self.state)

    @property
    def state_label(self) -> str:
        if self.state == ReviewState.SKIPPED_NO_DELTA:
            return "Skipped"
        return self.state.value.replace("_", " ").title()


@dataclass(frozen=True, slots=True)
class ReviewerRenderContext:
    title: str
    subtitle: str
    edition_name: str
    issue_number: int
    published_html: str
    published_html_uri: str
    anticipated_questions: tuple[ReviewerAnticipatedQuestion, ...]
    trust_editorial_rows: tuple[ReviewerTrustRow, ...]
    trust_claim_extraction_rows: tuple[ReviewerTrustRow, ...]
    trust_autonomy_rows: tuple[ReviewerTrustRow, ...]
    trust_attention_gap_rows: tuple[ReviewerAttentionGapRow, ...]
    owner_vitality_rows: tuple[ReviewerVitalityRow, ...]
    coverage_gap_rows: tuple[ReviewerTrackedEntryRow, ...]
    coverage_gap_window_days: int
    telemetry_rows: tuple[ReviewerTrackedEntryRow, ...]
    open_claim_rows: tuple[ReviewerTrackedEntryRow, ...]
    risk_rows: tuple[ReviewerTrackedEntryRow, ...]
    milestone_timeline_rows: tuple[ReviewerMilestoneTimelineRow, ...]
    milestone_rows: tuple[ReviewerTrackedEntryRow, ...]
    cascade_rows: tuple[ReviewerTrackedEntryRow, ...]
    signal_thread_rows: tuple[ReviewerSignalThreadRow, ...]
    decision_rows: tuple[ReviewerTrackedEntryRow, ...]
    assumption_rows: tuple[ReviewerTrackedEntryRow, ...]
    reference_doc_rows: tuple[ReviewerTrackedEntryRow, ...]
    action_rows: tuple[ReviewerTrackedEntryRow, ...]
    issue_rows: tuple[ReviewerTrackedEntryRow, ...]
    open_ask_rows: tuple[ReviewerTrackedEntryRow, ...]
    status_chips: tuple[ReviewerStatusChip, ...]
    sections: tuple[ReviewerSectionData, ...]
    dependency_lifecycle_rows: tuple[ReviewerTrackedEntryRow, ...] = ()
    calibration_rows: tuple[WorkstreamCalibration, ...] = ()
    persona_coverage: dict[str, Any] | None = None


def build_render_payload(context: ReviewerRenderContext) -> dict[str, Any]:
    return {
        "title": context.title,
        "subtitle": context.subtitle,
        "edition_name": context.edition_name,
        "issue_number": context.issue_number,
        "published_html": context.published_html,
        "published_html_uri": context.published_html_uri,
        "anticipated_questions": context.anticipated_questions,
        "trust_editorial_rows": context.trust_editorial_rows,
        "trust_claim_extraction_rows": context.trust_claim_extraction_rows,
        "trust_autonomy_rows": context.trust_autonomy_rows,
        "trust_attention_gap_rows": context.trust_attention_gap_rows,
        "owner_vitality_rows": context.owner_vitality_rows,
        "coverage_gap_rows": context.coverage_gap_rows,
        "coverage_gap_window_days": context.coverage_gap_window_days,
        "telemetry_rows": context.telemetry_rows,
        "open_claim_rows": context.open_claim_rows,
        "risk_rows": context.risk_rows,
        "milestone_timeline_rows": context.milestone_timeline_rows,
        "milestone_rows": context.milestone_rows,
        "cascade_rows": context.cascade_rows,
        "signal_thread_rows": context.signal_thread_rows,
        "decision_rows": context.decision_rows,
        "assumption_rows": context.assumption_rows,
        "reference_doc_rows": context.reference_doc_rows,
        "action_rows": context.action_rows,
        "issue_rows": context.issue_rows,
        "open_ask_rows": context.open_ask_rows,
        "status_chips": context.status_chips,
        "sections": context.sections,
        "dependency_lifecycle_rows": context.dependency_lifecycle_rows,
        "calibration_rows": context.calibration_rows,
        "persona_coverage": context.persona_coverage,
    }


class ReviewerRenderer:
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

    def render(self, context: ReviewerRenderContext) -> str:
        try:
            template = self.environment.get_template("base.reviewer.j2")
        except TemplateNotFound as exc:
            raise RenderError("Missing template: base.reviewer.j2") from exc
        return template.render(**build_render_payload(context)).strip() + "\n"

    def render_fragment(self, template_name: str, **context: Any) -> str:
        try:
            return self.environment.get_template(template_name).render(**context)
        except TemplateNotFound as exc:
            raise RenderError(f"Missing template: {template_name}") from exc
