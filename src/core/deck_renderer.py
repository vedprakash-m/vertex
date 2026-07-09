from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateNotFound

from src.core.exceptions import RenderError
from src.core.html_renderer import REPORTS_ROOT, TEMPLATES_ROOT
from src.core.jinja_filters import JINJA_FILTERS, JINJA_GLOBALS
from src.core.models import RiskLevel


@dataclass(frozen=True, slots=True)
class DeckHealthRow:
    dimension_name: str
    risk: RiskLevel
    summary: str


@dataclass(frozen=True, slots=True)
class DeckTopRiskRow:
    text: str
    risk: RiskLevel | None
    delta_text: str | None
    work_item_id: int | None


@dataclass(frozen=True, slots=True)
class DeckChangeRow:
    text: str


@dataclass(frozen=True, slots=True)
class DeckDataRow:
    label: str
    value: str


@dataclass(frozen=True, slots=True)
class DeckIssueRow:
    title: str
    detail: str
    href: str | None = None


@dataclass(frozen=True, slots=True)
class DeckRiskRow:
    title: str
    detail: str
    evidence_truth_level: str | None = None
    evidence_disputed: bool = False
    evidence_stale: bool = False


@dataclass(frozen=True, slots=True)
class DeckDecisionRow:
    title: str
    detail: str
    evidence_truth_level: str | None = None
    evidence_disputed: bool = False
    evidence_stale: bool = False


@dataclass(frozen=True, slots=True)
class DeckAssumptionRow:
    title: str
    detail: str
    evidence_truth_level: str | None = None
    evidence_disputed: bool = False
    evidence_stale: bool = False


@dataclass(frozen=True, slots=True)
class DeckDependencyProposalRow:
    title: str
    detail: str


@dataclass(frozen=True, slots=True)
class DeckMilestoneRow:
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
class DeckAskRow:
    title: str
    detail: str


@dataclass(frozen=True, slots=True)
class DeckRenderContext:
    issue_number: int
    issue_date_label: str
    health_rows: tuple[DeckHealthRow, ...]
    top_risk_rows: tuple[DeckTopRiskRow, ...]
    change_rows: tuple[DeckChangeRow, ...]
    data_rows: tuple[DeckDataRow, ...]
    open_ask_rows: tuple[DeckAskRow, ...]
    closed_ask_rows: tuple[DeckAskRow, ...]
    telemetry_summary: str | None = None
    telemetry_confidence: str | None = None
    charter_lines: tuple[str, ...] = ()
    open_risk_rows: tuple[DeckRiskRow, ...] = ()
    key_decision_rows: tuple[DeckDecisionRow, ...] = ()
    key_assumption_rows: tuple[DeckAssumptionRow, ...] = ()
    dependency_proposal_rows: tuple[DeckDependencyProposalRow, ...] = ()
    open_issue_rows: tuple[DeckIssueRow, ...] = ()
    milestone_rows: tuple[DeckMilestoneRow, ...] = ()


def build_render_payload(context: DeckRenderContext) -> dict[str, Any]:
    return {
        "issue_number": context.issue_number,
        "issue_date_label": context.issue_date_label,
        "health_rows": context.health_rows,
        "top_risk_rows": context.top_risk_rows,
        "change_rows": context.change_rows,
        "data_rows": context.data_rows,
        "telemetry_summary": context.telemetry_summary,
        "telemetry_confidence": context.telemetry_confidence,
        "charter_lines": context.charter_lines,
        "open_risk_rows": context.open_risk_rows,
        "dependency_proposal_rows": context.dependency_proposal_rows,
        "open_issue_rows": context.open_issue_rows,
        "key_decision_rows": context.key_decision_rows,
        "key_assumption_rows": context.key_assumption_rows,
        "open_ask_rows": context.open_ask_rows,
        "closed_ask_rows": context.closed_ask_rows,
        "milestone_rows": context.milestone_rows,
    }


class DeckRenderer:
    def __init__(
        self,
        edition_name: str,
        reports_root: Path = REPORTS_ROOT,
        templates_root: Path = TEMPLATES_ROOT,
    ) -> None:
        search_paths = [str(reports_root / edition_name / "templates"), str(templates_root)]
        self.environment = Environment(
            loader=FileSystemLoader(search_paths),
            autoescape=False,
            trim_blocks=True,
            lstrip_blocks=True,
            undefined=StrictUndefined,
        )
        self.environment.filters.update(JINJA_FILTERS)
        self.environment.globals.update(JINJA_GLOBALS)

    def render(self, context: DeckRenderContext) -> str:
        try:
            template = self.environment.get_template("archetypes/deck.j2")
        except TemplateNotFound as exc:
            raise RenderError("Missing template: archetypes/deck.j2") from exc
        return template.render(**build_render_payload(context)).strip() + "\n"

    def render_fragment(self, template_name: str, **context: Any) -> str:
        try:
            return self.environment.get_template(template_name).render(**context)
        except TemplateNotFound as exc:
            raise RenderError(f"Missing template: {template_name}") from exc
