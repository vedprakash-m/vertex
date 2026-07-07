from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from datetime import datetime, timedelta, timezone
from html import escape
from io import StringIO
import json
from pathlib import Path
from typing import Literal

import typer
import yaml

from src.core.action_tracker import assess_action_staleness
from src.core.archive_store import find_latest_confirmed_entry, read_archive_index, read_scorecard_history
from src.core.capability_status import ProgramCapabilityStatus, latest_program_capability_reviewed_on, load_program_capability_status, summarize_program_capabilities, summarize_program_capability_reviews, summarize_program_capability_verification
from src.core.circuit_breaker import CircuitBreaker, CircuitBreakerState
from src.core.claim_tracker import load_open_claims, load_open_decision_asks
from src.core.communication_plan import CommunicationPlanEntry, load_communication_plan_entries
from src.core.dependency_graph import dependency_impact_text, dependency_source_label, dependency_target_label
from src.core.edition_resolver import _resolve_output_dir
from src.core.exceptions import ConfigError
from src.core.freshness_engine import build_freshness_report
from src.core.gather_state_store import build_gather_integration_summary, load_gather_state
from src.core.issue_projection import IssueProjection, build_issue_projection, issue_projection_confidence_label, issue_projection_source_label
from src.core.models import RiskLevel, WorkItem
from src.core.models_v2 import Dependency, RiskEntry, RiskStatus
from src.core.overrides_store import load_archived_overrides
from src.core.risk_register_engine import assess_risk_staleness, compute_risk_score
from src.core.scorecard_trends import ScorecardTrend, load_scorecard_trends
from src.core.signal_ranking import signal_source_family
from src.core.snapshot_store import read_snapshot
from src.core.store_factory import build_signal_store_for_program_id
from src.core.program_context import InvariantSeverity, load_program_context
from src.core.program_fact_store import (
    load_program_facts,
    project_action_items,
    project_dependencies,
    project_risk_entries,
)
from src.core.telemetry_summary import build_program_telemetry_summary


REPO_ROOT = Path(__file__).resolve().parents[2]
PROGRAMS_ROOT = REPO_ROOT / "programs"
_MANIFEST_NAME_RE = __import__("re").compile(r"^issue_(\d{3})\.manifest\.json$")

_DEFAULT_STALE_WINDOW = timedelta(days=7)
_MAX_ACTIVE_ISSUE_HIGHLIGHTS = 2
_CADENCE_WINDOWS = {
    "daily": timedelta(days=1),
    "weekly": timedelta(days=7),
    "biweekly": timedelta(days=14),
    "monthly": timedelta(days=30),
    "quarterly": timedelta(days=91),
}


@dataclass(frozen=True, slots=True)
class FleetDependencySummary:
    direction: Literal["inbound", "outbound"]
    counterpart_program_id: str
    dependency_type: str
    status: str
    source_label: str
    target_label: str
    impact: str

    def to_payload(self) -> dict[str, object]:
        return {
            "direction": self.direction,
            "counterpart_program_id": self.counterpart_program_id,
            "dependency_type": self.dependency_type,
            "status": self.status,
            "source_label": self.source_label,
            "target_label": self.target_label,
            "impact": self.impact,
        }


@dataclass(frozen=True, slots=True)
class FleetDependencyChainSummary:
    program_path: tuple[str, ...]
    route: str
    hop_count: int
    broken_hop_count: int

    def to_payload(self) -> dict[str, object]:
        return {
            "program_path": list(self.program_path),
            "route": self.route,
            "hop_count": self.hop_count,
            "broken_hop_count": self.broken_hop_count,
        }


@dataclass(frozen=True, slots=True)
class FleetIssueSummary:
    title: str
    detail: str
    href: str | None

    def to_payload(self) -> dict[str, object]:
        return {
            "title": self.title,
            "detail": self.detail,
            "href": self.href,
        }


@dataclass(frozen=True, slots=True)
class FleetRiskRegisterSummary:
    active_count: int
    stale_count: int
    highlight: str | None

    def to_payload(self) -> dict[str, object]:
        return {
            "active_count": self.active_count,
            "stale_count": self.stale_count,
            "highlight": self.highlight,
        }


@dataclass(frozen=True, slots=True)
class FleetDependencyHealthSummary:
    total_count: int
    inbound_count: int
    outbound_count: int
    broken_count: int
    highlight: str | None

    def to_payload(self) -> dict[str, object]:
        return {
            "total_count": self.total_count,
            "inbound_count": self.inbound_count,
            "outbound_count": self.outbound_count,
            "broken_count": self.broken_count,
            "highlight": self.highlight,
        }


@dataclass(frozen=True, slots=True)
class FleetNudgeSummary:
    config_status: Literal["ok", "warn", "fail"]
    last_generated_at: datetime | None
    section_count: int
    cooldown_item_count: int
    degraded_section_count: int
    core_gate_status: Literal["ok", "warn", "fail"]

    def to_payload(self) -> dict[str, object]:
        return {
            "config_status": self.config_status,
            "last_generated_at": self.last_generated_at.isoformat() if self.last_generated_at is not None else None,
            "section_count": self.section_count,
            "cooldown_item_count": self.cooldown_item_count,
            "degraded_section_count": self.degraded_section_count,
            "core_gate_status": self.core_gate_status,
        }


@dataclass(frozen=True, slots=True)
class FleetProgramSummary:
    program_id: str
    program_name: str
    primary_edition: str
    latest_issue_number: int | None
    latest_confirmed_at: datetime | None
    gather_integration_summary: str | None
    gather_integration_details: tuple[dict[str, object], ...]
    overall_risk: RiskLevel
    telemetry_summary: str | None
    active_issue_count: int
    risk_register: FleetRiskRegisterSummary
    dependency_health: FleetDependencyHealthSummary
    active_issue_summaries: tuple[FleetIssueSummary, ...]
    trend: Literal["improving", "stable", "worsening"]
    trend_detail: str | None
    stale: bool
    staleness_reason: str
    top_items: tuple[str, ...]
    cross_program_dependencies: tuple[FleetDependencySummary, ...]
    dependency_heat_chains: tuple[FleetDependencyChainSummary, ...]
    ai_safety_summary: str | None
    ado_breaker_summary: str | None
    capability_summary: str | None
    capability_review_summary: str | None
    capability_verification_summary: str | None
    latest_capability_reviewed_on: date | None
    capabilities: tuple[ProgramCapabilityStatus, ...]
    lifecycle_state: Literal["active", "onboarding"] = "active"
    context_maturity_level: int = 0
    context_invariant_errors: int = 0
    context_stale_file_count: int = 0
    nudge: FleetNudgeSummary | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "program_id": self.program_id,
            "program_name": self.program_name,
            "primary_edition": self.primary_edition,
            "latest_issue_number": self.latest_issue_number,
            "latest_confirmed_at": self.latest_confirmed_at.isoformat() if self.latest_confirmed_at is not None else None,
            "gather_integration_summary": self.gather_integration_summary,
            "gather_integration_details": list(self.gather_integration_details),
            "overall_risk": self.overall_risk.value,
            "telemetry_summary": self.telemetry_summary,
            "active_issue_count": self.active_issue_count,
            "risk_register": self.risk_register.to_payload(),
            "dependency_health": self.dependency_health.to_payload(),
            "active_issue_summaries": [entry.to_payload() for entry in self.active_issue_summaries],
            "trend": self.trend,
            "trend_detail": self.trend_detail,
            "stale": self.stale,
            "staleness_reason": self.staleness_reason,
            "top_items": list(self.top_items),
            "cross_program_dependencies": [entry.to_payload() for entry in self.cross_program_dependencies],
            "dependency_heat_chains": [entry.to_payload() for entry in self.dependency_heat_chains],
            "ai_safety_summary": self.ai_safety_summary,
            "ado_breaker_summary": self.ado_breaker_summary,
            "capability_summary": self.capability_summary,
            "capability_review_summary": self.capability_review_summary,
            "capability_verification_summary": self.capability_verification_summary,
            "latest_capability_reviewed_on": self.latest_capability_reviewed_on.isoformat() if self.latest_capability_reviewed_on is not None else None,
            "capabilities": [entry.to_payload() for entry in self.capabilities],
            "lifecycle_state": self.lifecycle_state,
            "context_maturity_level": self.context_maturity_level,
            "context_invariant_errors": self.context_invariant_errors,
            "context_stale_file_count": self.context_stale_file_count,
            "nudge": self.nudge.to_payload() if self.nudge is not None else None,
        }


@dataclass(frozen=True, slots=True)
class FleetReport:
    generated_at: datetime
    programs: tuple[FleetProgramSummary, ...]

    @property
    def stale_program_count(self) -> int:
        return sum(1 for program in self.programs if program.stale)

    def to_payload(self) -> dict[str, object]:
        return {
            "generated_at": self.generated_at.isoformat(),
            "program_count": len(self.programs),
            "stale_program_count": self.stale_program_count,
            "programs": [program.to_payload() for program in self.programs],
        }


def fleet_command(
    format: str = typer.Option("human", "--format", help="Output format: human, json, csv, md, or html."),
    programs: str | None = typer.Option(None, "--programs", help="Optional comma-separated program ids."),
) -> None:
    selected_program_ids = _parse_program_ids(programs)
    report = build_fleet_report(selected_program_ids=selected_program_ids)

    if format == "json":
        typer.echo(json.dumps(report.to_payload(), indent=2, sort_keys=True))
        raise typer.Exit(code=0)
    if format == "csv":
        typer.echo(render_fleet_csv(report), nl=False)
        raise typer.Exit(code=0)
    if format == "human":
        typer.echo(render_fleet_report(report))
        raise typer.Exit(code=0)
    if format == "md":
        typer.echo(render_fleet_markdown(report))
        raise typer.Exit(code=0)
    if format == "html":
        typer.echo(render_fleet_html(report))
        raise typer.Exit(code=0)
    raise typer.BadParameter("--format must be 'human', 'json', 'csv', 'md', or 'html'.")


def build_fleet_report(
    *,
    selected_program_ids: tuple[str, ...] | None = None,
    as_of: datetime | None = None,
    programs_root: Path | None = None,
) -> FleetReport:
    now = as_of or datetime.now(timezone.utc)
    resolved_programs_root = programs_root or PROGRAMS_ROOT
    selected = set(selected_program_ids or ())
    cross_program_dependencies_raw = _load_cross_program_dependencies(resolved_programs_root)
    cross_program_dependencies = _build_cross_program_dependency_index(cross_program_dependencies_raw)
    dependency_heat_index = _build_dependency_heat_index(cross_program_dependencies_raw)
    programs: list[FleetProgramSummary] = []

    for program_dir in sorted(resolved_programs_root.iterdir(), key=lambda entry: entry.name.lower()):
        if not program_dir.is_dir() or not (program_dir / "program.yaml").exists():
            continue
        if selected and program_dir.name not in selected:
            continue
        summary = _build_program_summary(
            program_dir=program_dir,
            programs_root=resolved_programs_root,
            as_of=now,
            cross_program_dependencies=cross_program_dependencies.get(program_dir.name, ()),
            dependency_heat_chains=dependency_heat_index.get(program_dir.name, ()),
        )
        if summary is not None:
            programs.append(summary)

    return FleetReport(generated_at=now, programs=tuple(programs))


def render_fleet_report(report: FleetReport) -> str:
    lines = [
        f"Fleet Summary — {len(report.programs)} program{'s' if len(report.programs) != 1 else ''} | {report.stale_program_count} stale",
    ]
    if not report.programs:
        lines.append("No confirmed program archives found.")
        return "\n".join(lines)

    for program in report.programs:
        lines.extend(
            (
                "",
                f"- {program.program_name} ({program.program_id})",
                f"  Primary edition: {program.primary_edition}",
                f"  State: {program.lifecycle_state}",
                f"  Capabilities: {program.capability_summary or 'None'}",
                f"  Capability review: {program.capability_review_summary or 'unknown'}",
                f"  Capability verify: {program.capability_verification_summary or 'none pending'}",
                f"  Latest confirmed: {_latest_confirmed_label(program)}",
                f"  Gather: {program.gather_integration_summary or 'no optional integration failures recorded'}",
                f"  Overall risk: {program.overall_risk.value.upper()}",
                f"  Telemetry:     {program.telemetry_summary or 'None'}",
                f"  Active issues: {program.active_issue_count}",
                f"  Risk register: {program.risk_register.active_count} active, {program.risk_register.stale_count} stale",
                f"  Dependency health: {_render_dependency_health_summary(program.dependency_health)}",
                f"  Trend: {program.trend}{_detail_suffix(program.trend_detail)}",
                f"  Staleness: {program.staleness_reason}",
                f"  Context health: L{program.context_maturity_level} | {program.context_invariant_errors} invariant error(s) | {program.context_stale_file_count} stale file(s)",
            )
        )
        if program.ai_safety_summary:
            lines.append(f"  AI Safety:   {program.ai_safety_summary}")
        if program.ado_breaker_summary:
            lines.append(f"  ADO Breaker: {program.ado_breaker_summary}")
        lines.append(f"  Risk highlight: {program.risk_register.highlight or 'None'}")
        lines.append(f"  Dependency highlight: {program.dependency_health.highlight or 'None'}")
        lines.append("  Issue highlights:")
        if program.active_issue_summaries:
            lines.extend(f"    - {_render_issue_line(issue)}" for issue in program.active_issue_summaries)
        else:
            lines.append("    - None")
        lines.append("  Top 3:")
        if program.top_items:
            lines.extend(f"    - {item}" for item in program.top_items)
        else:
            lines.append("    - None")
        lines.append("  Cross-program dependencies:")
        if program.cross_program_dependencies:
            lines.extend(f"    - {_render_dependency_line(dependency)}" for dependency in program.cross_program_dependencies)
        else:
            lines.append("    - None")
        lines.append("  Dependency heat:")
        if program.dependency_heat_chains:
            lines.extend(f"    - {_render_dependency_heat_line(chain)}" for chain in program.dependency_heat_chains)
        else:
            lines.append("    - None")
    return "\n".join(lines)


def render_fleet_markdown(report: FleetReport) -> str:
    lines = [f"# Fleet Summary", "", f"Generated: {report.generated_at.isoformat()}", ""]
    if not report.programs:
        lines.append("No confirmed program archives found.")
        return "\n".join(lines)

    for program in report.programs:
        lines.extend(
            (
                f"## {program.program_name} ({program.program_id})",
                "",
                f"- Primary edition: {program.primary_edition}",
                f"- State: {program.lifecycle_state}",
                f"- Capabilities: {program.capability_summary or 'None'}",
                f"- Capability review: {program.capability_review_summary or 'unknown'}",
                f"- Capability verify: {program.capability_verification_summary or 'none pending'}",
                f"- Latest confirmed: {_latest_confirmed_label(program)}",
                f"- Gather: {program.gather_integration_summary or 'no optional integration failures recorded'}",
                f"- Overall risk: {program.overall_risk.value.upper()}",
                f"- Telemetry: {program.telemetry_summary or 'None'}",
                f"- Active issues: {program.active_issue_count}",
                f"- Risk register: {program.risk_register.active_count} active, {program.risk_register.stale_count} stale",
                f"- Risk highlight: {program.risk_register.highlight or 'None'}",
                f"- Dependency health: {_render_dependency_health_summary(program.dependency_health)}",
                f"- Dependency highlight: {program.dependency_health.highlight or 'None'}",
                f"- Trend: {program.trend}{_detail_suffix(program.trend_detail)}",
                f"- Staleness: {program.staleness_reason}",
                f"- Context health: L{program.context_maturity_level} | {program.context_invariant_errors} invariant error(s) | {program.context_stale_file_count} stale file(s)",
            )
        )
        if program.ai_safety_summary:
            lines.append(f"- AI Safety: {program.ai_safety_summary}")
        if program.ado_breaker_summary:
            lines.append(f"- ADO Breaker: {program.ado_breaker_summary}")
        lines.append("- Issue highlights:")
        if program.active_issue_summaries:
            lines.extend(f"  - {_render_issue_line(issue)}" for issue in program.active_issue_summaries)
        else:
            lines.append("  - None")
        lines.extend(
            (
                "- Top 3:",
            )
        )
        if program.top_items:
            lines.extend(f"  - {item}" for item in program.top_items)
        else:
            lines.append("  - None")
        lines.append("- Cross-program dependencies:")
        if program.cross_program_dependencies:
            lines.extend(f"  - {_render_dependency_line(dependency)}" for dependency in program.cross_program_dependencies)
        else:
            lines.append("  - None")
        lines.append("- Dependency heat:")
        if program.dependency_heat_chains:
            lines.extend(f"  - {_render_dependency_heat_line(chain)}" for chain in program.dependency_heat_chains)
        else:
            lines.append("  - None")
        lines.append("")
    return "\n".join(lines).rstrip()


def render_fleet_csv(report: FleetReport) -> str:
    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        (
            "generated_at",
            "program_id",
            "program_name",
            "primary_edition",
            "lifecycle_state",
            "capability_summary",
            "capability_review_summary",
            "capability_verification_summary",
            "latest_capability_reviewed_on",
            "capabilities_json",
            "gather_integration_summary",
            "gather_integration_details_json",
            "latest_issue_number",
            "latest_confirmed_at",
            "overall_risk",
            "telemetry_summary",
            "active_issue_count",
            "risk_register_active_count",
            "risk_register_stale_count",
            "risk_register_highlight",
            "dependency_total_count",
            "dependency_inbound_count",
            "dependency_outbound_count",
            "dependency_broken_count",
            "dependency_highlight",
            "trend",
            "trend_detail",
            "stale",
            "staleness_reason",
            "ai_safety_summary",
            "ado_breaker_summary",
            "top_items",
            "active_issue_summaries",
            "cross_program_dependencies",
            "dependency_heat_chains",
            "context_maturity_level",
            "context_invariant_errors",
            "context_stale_file_count",
        )
    )
    for program in report.programs:
        writer.writerow(
            (
                report.generated_at.isoformat(),
                program.program_id,
                program.program_name,
                program.primary_edition,
                program.lifecycle_state,
                program.capability_summary or "",
                program.capability_review_summary or "",
                program.capability_verification_summary or "",
                program.latest_capability_reviewed_on.isoformat() if program.latest_capability_reviewed_on is not None else "",
                json.dumps([entry.to_payload() for entry in program.capabilities], sort_keys=True),
                program.gather_integration_summary or "",
                json.dumps(list(program.gather_integration_details), sort_keys=True),
                program.latest_issue_number,
                program.latest_confirmed_at.isoformat() if program.latest_confirmed_at is not None else "",
                program.overall_risk.value,
                program.telemetry_summary or "",
                program.active_issue_count,
                program.risk_register.active_count,
                program.risk_register.stale_count,
                program.risk_register.highlight or "",
                program.dependency_health.total_count,
                program.dependency_health.inbound_count,
                program.dependency_health.outbound_count,
                program.dependency_health.broken_count,
                program.dependency_health.highlight or "",
                program.trend,
                program.trend_detail or "",
                "true" if program.stale else "false",
                program.staleness_reason,
                program.ai_safety_summary or "",
                program.ado_breaker_summary or "",
                "|".join(program.top_items),
                "|".join(_render_issue_csv(entry) for entry in program.active_issue_summaries),
                "|".join(_render_dependency_line(entry) for entry in program.cross_program_dependencies),
                "|".join(_render_dependency_heat_line(entry) for entry in program.dependency_heat_chains),
                program.context_maturity_level,
                program.context_invariant_errors,
                program.context_stale_file_count,
            )
        )
    return buffer.getvalue()


def render_fleet_html(report: FleetReport) -> str:
    rows: list[str] = []
    for program in report.programs:
        issue_html = (
            "<ul>" + "".join(f"<li>{_render_issue_html(issue)}</li>" for issue in program.active_issue_summaries) + "</ul>"
            if program.active_issue_summaries
            else "<p>None</p>"
        )
        risk_register_html = (
            f"<p>{program.risk_register.active_count} active, {program.risk_register.stale_count} stale</p>"
            f"<p>{escape(program.risk_register.highlight or 'None')}</p>"
        )
        dependency_health_html = (
            f"<p>{escape(_render_dependency_health_summary(program.dependency_health))}</p>"
            f"<p>{escape(program.dependency_health.highlight or 'None')}</p>"
        )
        top_items_html = "<ul>" + "".join(f"<li>{escape(item)}</li>" for item in program.top_items) + "</ul>" if program.top_items else "<p>None</p>"
        dependency_html = (
            dependency_health_html
            + "<ul>"
            + "".join(f"<li>{escape(_render_dependency_line(dependency))}</li>" for dependency in program.cross_program_dependencies)
            + "</ul>"
            if program.cross_program_dependencies
            else dependency_health_html + "<p>None</p>"
        )
        dependency_heat_html = (
            "<ul>" + "".join(f"<li>{escape(_render_dependency_heat_line(chain))}</li>" for chain in program.dependency_heat_chains) + "</ul>"
            if program.dependency_heat_chains
            else "<p>None</p>"
        )
        rows.append(
            "<tr>"
            f"<td>{escape(program.program_name)}</td>"
            f"<td>{escape(program.primary_edition)}</td>"
            f"<td>{escape(program.lifecycle_state)}</td>"
            f"<td>{escape(program.capability_summary or 'None')}</td>"
            f"<td>{escape(program.capability_review_summary or 'unknown')}</td>"
            f"<td>{escape(program.capability_verification_summary or 'none pending')}</td>"
            f"<td>{escape(_latest_confirmed_label(program))}</td>"
            f"<td>{escape(program.overall_risk.value.upper())}</td>"
            f"<td>{escape(program.telemetry_summary or 'None')}</td>"
            f"<td>{program.active_issue_count}</td>"
            f"<td>{risk_register_html}</td>"
            f"<td>{issue_html}</td>"
            f"<td>{escape(program.trend + _detail_suffix(program.trend_detail))}</td>"
            f"<td>{escape(program.staleness_reason)}</td>"
            f"<td>{escape(program.ai_safety_summary or 'None')}</td>"
            f"<td>{escape(program.ado_breaker_summary or 'None')}</td>"
            f"<td>{top_items_html}</td>"
            f"<td>{dependency_html}<p>Dependency heat</p>{dependency_heat_html}</td>"
            "</tr>"
        )

    body = (
        f"<p>No confirmed program archives found.</p>" if not report.programs else
        "<table border=\"1\" cellspacing=\"0\" cellpadding=\"6\">"
        "<thead><tr><th>Program</th><th>Edition</th><th>State</th><th>Capabilities</th><th>Capability review</th><th>Capability verify</th><th>Latest confirmed</th><th>Risk</th><th>Telemetry</th><th>Active issues</th><th>Risk register</th><th>Issue highlights</th><th>Trend</th><th>Staleness</th><th>AI safety</th><th>ADO breaker</th><th>Top 3</th><th>Cross-program dependencies</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )
    return (
        "<!DOCTYPE html>"
        "<html><head><meta charset=\"utf-8\"><title>Fleet Summary</title></head>"
        f"<body><h1>Fleet Summary</h1><p>Generated: {escape(report.generated_at.isoformat())}</p>{body}</body></html>"
    )


def _build_program_summary(
    *,
    program_dir: Path,
    programs_root: Path,
    as_of: datetime,
    cross_program_dependencies: tuple[FleetDependencySummary, ...],
    dependency_heat_chains: tuple[FleetDependencyChainSummary, ...],
) -> FleetProgramSummary | None:
    program_id = program_dir.name
    program_document = yaml.safe_load((program_dir / "program.yaml").read_text(encoding="utf-8")) or {}
    capability_statuses = load_program_capability_status(
        program_id,
        programs_root=programs_root,
        program_document=program_document,
    )
    archive_root = program_dir / "archive"
    primary_edition = _select_primary_edition(program_document, archive_root)
    if primary_edition is None:
        return None

    gather_state = load_gather_state(program_id, programs_root=programs_root)
    gather_integration_summary = build_gather_integration_summary(gather_state)
    gather_integration_details: tuple[dict[str, object], ...] = tuple(
        {
            "source": detail.source,
            "stage": detail.stage,
            "retryable": detail.retryable,
            "message": detail.message,
            "operator_action": detail.operator_action,
        }
        for detail in (gather_state.integration_error_details if gather_state is not None else ())
    )

    archive_index = read_archive_index(primary_edition, archive_root=archive_root)
    latest_confirmed = find_latest_confirmed_entry(archive_index)
    scorecard_history = read_scorecard_history(primary_edition, archive_root=archive_root)
    latest_entries = tuple(
        entry
        for entry in scorecard_history
        if latest_confirmed is not None and _issue_number(entry.get("issue_number")) == latest_confirmed.issue_number
    )
    current_dimensions = {
        (str(entry.get("scorecard_name") or "").strip(), str(entry.get("dimension") or "").strip()): RiskLevel.from_string(str(entry.get("risk") or ""))
        for entry in latest_entries
        if str(entry.get("scorecard_name") or "").strip() and str(entry.get("dimension") or "").strip() and str(entry.get("risk") or "").strip()
    }
    telemetry_summary = build_program_telemetry_summary(
        program_id,
        programs_root=programs_root,
        as_of=as_of,
    )
    primary_plan_entry = _find_communication_plan_entry(program_document, primary_edition)
    risk_register = _build_fleet_risk_register_summary(
        program_id=program_id,
        as_of=as_of,
        programs_root=programs_root,
    )
    dependency_health = _build_fleet_dependency_health_summary(cross_program_dependencies)
    if latest_confirmed is None:
        overall_risk = RiskLevel.UNKNOWN
        trend: Literal["improving", "stable", "worsening"] = "stable"
        trend_detail = None
        stale = False
        staleness_reason = "onboarding — no confirmed issue yet"
        active_issue_projections: tuple[IssueProjection, ...] = ()
        top_items: tuple[str, ...] = ()
        lifecycle_state: Literal["active", "onboarding"] = "onboarding"
    else:
        overall_risk = max(current_dimensions.values(), key=_risk_rank) if current_dimensions else RiskLevel.UNKNOWN
        trends: dict[tuple[str, str], ScorecardTrend] = load_scorecard_trends(primary_edition, current_dimensions, archive_root=archive_root) if current_dimensions else {}
        prior_issue_number = _previous_issue_number(scorecard_history, latest_confirmed.issue_number)
        prior_overall_risk = _overall_risk_for_issue(scorecard_history, prior_issue_number) if prior_issue_number is not None else None
        trend = _aggregate_trend(current=overall_risk, prior=prior_overall_risk) if current_dimensions else "stable"
        trend_detail = _aggregate_trend_detail(trends) if current_dimensions else None
        stale, staleness_reason = _compute_staleness(
            primary_edition,
            latest_confirmed.generated_at,
            as_of,
            cadence_label=primary_plan_entry.cadence if primary_plan_entry is not None else None,
        )
        active_issue_projections = _build_active_issue_projections(
            program_document=program_document,
            program_id=program_id,
            latest_confirmed=latest_confirmed,
            as_of=as_of,
            programs_root=programs_root,
        )
        archived_overrides = load_archived_overrides(primary_edition, latest_confirmed.issue_number, archive_root=archive_root)
        top_items = (
            tuple(entry.text.strip() for entry in archived_overrides.top_3_now if entry.text.strip())[:3]
            if archived_overrides is not None
            else ()
        )
        lifecycle_state = "active"

    # §17.4: Context health columns
    try:
        _pc = load_program_context(program_id, programs_root=programs_root, raise_on_error=False)
        _ctx_maturity = _pc.maturity_level.value
        _ctx_errors = sum(1 for v in _pc.invariant_violations if v.severity == InvariantSeverity.ERROR)
        _ctx_stale = len(_pc.staleness_flags)
    except Exception:
        _ctx_maturity = 0
        _ctx_errors = 0
        _ctx_stale = 0

    return FleetProgramSummary(
        program_id=program_id,
        program_name=str(program_document.get("name") or program_id),
        primary_edition=primary_edition,
        latest_issue_number=(latest_confirmed.issue_number if latest_confirmed is not None else None),
        latest_confirmed_at=(latest_confirmed.generated_at if latest_confirmed is not None else None),
        gather_integration_summary=gather_integration_summary,
        gather_integration_details=gather_integration_details,
        overall_risk=overall_risk,
        telemetry_summary=telemetry_summary,
        active_issue_count=len(active_issue_projections),
        risk_register=risk_register,
        dependency_health=dependency_health,
        active_issue_summaries=_build_active_issue_summaries(active_issue_projections),
        trend=trend,
        trend_detail=trend_detail,
        stale=stale,
        staleness_reason=staleness_reason,
        top_items=top_items,
        cross_program_dependencies=cross_program_dependencies,
        dependency_heat_chains=dependency_heat_chains,
        ai_safety_summary=_build_fleet_ai_safety_summary(_resolve_output_dir(program_dir, primary_edition)),
        ado_breaker_summary=_build_fleet_ado_breaker_summary(_resolve_output_dir(program_dir, primary_edition)),
        capability_summary=summarize_program_capabilities(capability_statuses),
        capability_review_summary=summarize_program_capability_reviews(capability_statuses),
        capability_verification_summary=summarize_program_capability_verification(capability_statuses),
        latest_capability_reviewed_on=latest_program_capability_reviewed_on(capability_statuses),
        capabilities=capability_statuses,
        lifecycle_state=lifecycle_state,
        context_maturity_level=_ctx_maturity,
        context_invariant_errors=_ctx_errors,
        context_stale_file_count=_ctx_stale,
        nudge=_build_fleet_nudge_summary(program_id, programs_root=programs_root),
    )


def _build_fleet_nudge_summary(program_id: str, *, programs_root: Path) -> FleetNudgeSummary | None:
    import json as _json  # noqa: PLC0415
    edition_path = programs_root / program_id / "editions" / f"{program_id}_nudge.yaml"
    if not edition_path.exists():
        return None

    try:
        raw = yaml.safe_load(edition_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return FleetNudgeSummary(
            config_status="fail",
            last_generated_at=None,
            section_count=0,
            cooldown_item_count=0,
            degraded_section_count=0,
            core_gate_status="fail",
        )

    fh = raw.get("full_hygiene") or {}
    sections = fh.get("sections") or []
    has_sections = isinstance(sections, list) and len(sections) > 0
    has_legacy = any(k in fh for k in ("ramp_p1_tag", "post_ramp_tag", "section_a_tag", "area_paths"))
    if has_sections:
        config_status: Literal["ok", "warn", "fail"] = "ok"
        section_count = len(sections)
    elif has_legacy:
        config_status = "warn"
        section_count = 0
    else:
        config_status = "fail"
        section_count = 0

    # Read state file for cooldown count — new layout then legacy fallback
    from src.core.edition_resolver import get_nudge_paths  # noqa: PLC0415
    _np = get_nudge_paths(program_id, programs_root=programs_root)
    _legacy_state = programs_root / program_id / "nudge_state.json"
    state_path = _np.state_path if _np.state_path.exists() else _legacy_state
    cooldown_count = 0
    if state_path.exists():
        try:
            state_raw = _json.loads(state_path.read_text(encoding="utf-8"))
            cooldown_count = sum(
                1 for k in state_raw
                if k != "schema_version" and (k.startswith("item:") or k.isdigit())
            )
        except Exception:
            pass

    # Read latest audit for last_generated_at and degraded_section_count — new layout then legacy fallback
    from src.core.edition_resolver import get_legacy_nudge_output  # noqa: PLC0415
    _legacy_output = get_legacy_nudge_output(program_id, programs_root=programs_root)
    audit_path = _np.audit_path if _np.audit_path.exists() else _legacy_output / "nudge_audit.jsonl"
    last_generated_at: datetime | None = None
    degraded_section_count = 0
    if audit_path.exists():
        try:
            lines = audit_path.read_text(encoding="utf-8", errors="replace").splitlines()
            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                event = _json.loads(line)
                et = event.get("event_type", "")
                if et in ("nudge_generated", "dry_run"):
                    ts = event.get("generated_at") or event.get("timestamp")
                    if ts:
                        try:
                            last_generated_at = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                        except ValueError:
                            pass
                    degraded_section_count = len(event.get("degraded_section_ids") or [])
                    break
        except Exception:
            pass

    # NQ-1..NQ-4 core gate
    from src.commands.doctor_checks.nudge_checks import run_nudge_doctor  # noqa: PLC0415
    checks = run_nudge_doctor(program_id, programs_root=programs_root, templates_root=None)
    core_checks = [c for c in checks if any(f"NQ-{i}" in c.label for i in range(1, 5))]
    if any(c.status == "fail" for c in core_checks):
        core_gate: Literal["ok", "warn", "fail"] = "fail"
    elif any(c.status == "warn" for c in core_checks):
        core_gate = "warn"
    else:
        core_gate = "ok"

    return FleetNudgeSummary(
        config_status=config_status,
        last_generated_at=last_generated_at,
        section_count=section_count,
        cooldown_item_count=cooldown_count,
        degraded_section_count=degraded_section_count,
        core_gate_status=core_gate,
    )


def _build_fleet_ai_safety_summary(output_dir: Path) -> str | None:
    latest_manifest = _load_latest_manifest(output_dir)
    if latest_manifest is None:
        return None
    return _build_fleet_manifest_ai_safety_summary(latest_manifest)


def _load_latest_manifest(output_dir: Path) -> Path | None:
    manifests: list[tuple[Path, int]] = []
    if output_dir.exists():
        for path in output_dir.glob("issue_*/issue_*.manifest.json"):
            match = _MANIFEST_NAME_RE.fullmatch(path.name)
            if match is not None:
                manifests.append((path, int(match.group(1))))
    if not manifests:
        return None
    path, _ = max(manifests, key=lambda item: item[1])
    return path


def _build_fleet_manifest_ai_safety_summary(manifest_path: Path) -> str | None:
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return f"malformed manifest at {_display_fleet_path(manifest_path)}"
    if not isinstance(payload, dict):
        return f"malformed manifest at {_display_fleet_path(manifest_path)}"

    metadata = payload.get("metadata")
    if metadata is None:
        return None
    if not isinstance(metadata, dict):
        return f"malformed manifest at {_display_fleet_path(manifest_path)}"

    ai_safety = metadata.get("ai_safety")
    if ai_safety is None:
        return None
    if not isinstance(ai_safety, dict):
        return f"malformed manifest at {_display_fleet_path(manifest_path)}"
    if not ai_safety:
        return None

    enabled = bool(ai_safety.get("enabled"))
    budget_usd = ai_safety.get("budget_usd")
    spent_usd = ai_safety.get("spent_usd")
    ai_calls = ai_safety.get("ai_calls")
    within_budget = ai_safety.get("within_budget")
    trace_run_id = ai_safety.get("trace_run_id")

    if not enabled and not ai_calls and not spent_usd:
        return "disabled"

    parts: list[str] = []
    if isinstance(ai_calls, int):
        parts.append(f"{ai_calls} AI call{'s' if ai_calls != 1 else ''}")
    if isinstance(spent_usd, (int, float)) and isinstance(budget_usd, (int, float)):
        posture = "within budget" if bool(within_budget) else "budget exceeded"
        parts.append(f"${float(spent_usd):.6f} / ${float(budget_usd):.6f} ({posture})")
    if isinstance(trace_run_id, str) and trace_run_id.strip():
        parts.append(f"trace {trace_run_id.strip()}")
    return "; ".join(parts) or None


def _build_fleet_ado_breaker_summary(output_dir: Path) -> str | None:
    state_path = output_dir / ".ado_breaker.json"
    if not state_path.exists():
        return None

    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return f"malformed state at {_display_fleet_path(state_path)}"

    if _fleet_breaker_payload_is_malformed(payload):
        return f"malformed state at {_display_fleet_path(state_path)}"

    snapshot = CircuitBreaker(state_path=state_path).get_state()
    if snapshot.state == CircuitBreakerState.CLOSED:
        return None

    detail = f"{snapshot.state.value}; failure_count={snapshot.failure_count}"
    if snapshot.last_opened_at is not None:
        detail += f"; last_opened_at={snapshot.last_opened_at.isoformat()}"
    if snapshot.state == CircuitBreakerState.OPEN:
        detail += "; live freshness ADO requests gated"
    elif snapshot.state == CircuitBreakerState.HALF_OPEN:
        detail += "; recovery probe in progress"
    return detail


def _fleet_breaker_payload_is_malformed(payload: object) -> bool:
    if not isinstance(payload, dict):
        return True
    try:
        CircuitBreakerState(str(payload.get("state", CircuitBreakerState.CLOSED.value)))
        int(payload.get("failure_count", 0))
    except (TypeError, ValueError):
        return True

    return any(
        not _fleet_breaker_timestamp_is_valid(payload.get(key))
        for key in ("last_failure_at", "last_opened_at", "last_success_at")
    )


def _fleet_breaker_timestamp_is_valid(value: object) -> bool:
    if value is None:
        return True
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def _display_fleet_path(path: Path) -> str:
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(path)


def _build_active_issue_projections(
    *,
    program_document: dict[str, object],
    program_id: str,
    latest_confirmed,
    as_of: datetime,
    programs_root: Path,
) -> tuple[IssueProjection, ...]:
    snapshot_items = _load_snapshot_work_items(latest_confirmed)
    program_facts = load_program_facts(program_id, programs_root=programs_root)
    freshness_report = build_freshness_report(
        current_items=snapshot_items,
        issue_number=latest_confirmed.issue_number,
        as_of=as_of,
        stale_warn_days=14,
        stale_block_days=21,
        previous_snapshot=None,
        previous_notification_state=None,
        program_context=None,
        workstream_narrative_history={},
    )
    overdue_actions = assess_action_staleness(project_action_items(program_facts), as_of.date())
    signal_store = build_signal_store_for_program_id(program_id, programs_root=programs_root)
    icm_signals = tuple(
        signal
        for signal in signal_store.read(
            program_id,
            start=as_of - timedelta(days=30),
            end=as_of,
        )
        if signal_source_family(signal.source) == "icm"
    )
    return build_issue_projection(
        items=snapshot_items,
        freshness_report=freshness_report,
        icm_signals=icm_signals,
        open_asks=load_open_decision_asks(program_id, programs_root=programs_root),
        overdue_actions=overdue_actions,
        open_claims=load_open_claims(program_id, programs_root=programs_root),
        risk_entries=project_risk_entries(program_facts),
        ado_item_base_url=_ado_item_base_url_from_program(program_document),
    )


def _build_active_issue_summaries(
    issue_projections: tuple[IssueProjection, ...],
) -> tuple[FleetIssueSummary, ...]:
    return tuple(
        FleetIssueSummary(
            title=entry.summary,
            detail=_format_fleet_issue_detail(entry),
            href=entry.ado_url,
        )
        for entry in issue_projections[:_MAX_ACTIVE_ISSUE_HIGHLIGHTS]
    )


def _build_fleet_risk_register_summary(
    *,
    program_id: str,
    as_of: datetime,
    programs_root: Path,
) -> FleetRiskRegisterSummary:
    program_facts = load_program_facts(program_id, programs_root=programs_root)
    active_risks = tuple(
        risk
        for risk in project_risk_entries(program_facts)
        if risk.status in {RiskStatus.OPEN, RiskStatus.ESCALATED}
    )
    if not active_risks:
        return FleetRiskRegisterSummary(active_count=0, stale_count=0, highlight=None)

    stale_count = sum(1 for risk in active_risks if assess_risk_staleness(risk, as_of.date()))
    highest_risk = sorted(
        active_risks,
        key=lambda risk: (
            0 if risk.status == RiskStatus.ESCALATED else 1,
            -compute_risk_score(risk),
            risk.title.lower(),
        ),
    )[0]
    return FleetRiskRegisterSummary(
        active_count=len(active_risks),
        stale_count=stale_count,
        highlight=_format_fleet_risk_highlight(highest_risk, as_of=as_of),
    )


def _format_fleet_risk_highlight(
    risk: RiskEntry,
    *,
    as_of: datetime,
) -> str:
    freshness = "stale" if assess_risk_staleness(risk, as_of.date()) else "current"
    return (
        f"{risk.title} — {risk.status.value.upper()} | score {compute_risk_score(risk)} | "
        f"{freshness} | owner {risk.owner_alias}"
    )


def _build_fleet_dependency_health_summary(
    dependencies: tuple[FleetDependencySummary, ...],
) -> FleetDependencyHealthSummary:
    if not dependencies:
        return FleetDependencyHealthSummary(
            total_count=0,
            inbound_count=0,
            outbound_count=0,
            broken_count=0,
            highlight=None,
        )

    broken_dependencies = tuple(dependency for dependency in dependencies if dependency.status == "broken")
    highlight_source = broken_dependencies[0] if broken_dependencies else None
    return FleetDependencyHealthSummary(
        total_count=len(dependencies),
        inbound_count=sum(1 for dependency in dependencies if dependency.direction == "inbound"),
        outbound_count=sum(1 for dependency in dependencies if dependency.direction == "outbound"),
        broken_count=len(broken_dependencies),
        highlight=_render_dependency_line(highlight_source) if highlight_source is not None else None,
    )


def _format_fleet_issue_detail(entry: IssueProjection) -> str:
    details = [issue_projection_source_label(entry), entry.severity.upper(), issue_projection_confidence_label(entry)]
    if entry.owner_alias is not None:
        details.append(f"owner {entry.owner_alias}")
    if entry.workstream_id is not None:
        details.append(f"workstream {entry.workstream_id}")
    if entry.linked_entity_ids:
        details.append(f"linked {', '.join(entry.linked_entity_ids)}")
    return " | ".join(details)


def _render_issue_line(issue: FleetIssueSummary) -> str:
    if issue.href is None:
        return f"{issue.title} — {issue.detail}"
    return f"[{issue.title}]({issue.href}) — {issue.detail}"


def _render_issue_html(issue: FleetIssueSummary) -> str:
    title = escape(issue.title)
    detail = escape(issue.detail)
    if issue.href is None:
        return f"{title} — {detail}"
    return f'<a href="{escape(issue.href)}">{title}</a> — {detail}'


def _render_issue_csv(issue: FleetIssueSummary) -> str:
    if issue.href is None:
        return f"{issue.title} — {issue.detail}"
    return f"{issue.title} — {issue.detail} — {issue.href}"


def _ado_item_base_url_from_program(program_document: dict[str, object]) -> str | None:
    ado = program_document.get("ado")
    if not isinstance(ado, dict):
        return None
    organization = str(ado.get("organization") or "").strip()
    project = str(ado.get("project") or "").strip()
    if not organization or not project:
        return None
    return f"https://dev.azure.com/{organization}/{project}/_workitems/edit"


def _load_snapshot_work_items(latest_confirmed) -> tuple[WorkItem, ...]:
    if latest_confirmed.snapshot_path is None:
        return ()

    snapshot_path = Path(latest_confirmed.snapshot_path)
    if not snapshot_path.exists():
        return ()

    snapshot = read_snapshot(snapshot_path)
    return tuple(
        WorkItem(
            id=item.id,
            type=item.type,
            title=item.title,
            state=item.state,
            assigned_to=item.assigned_to,
            assigned_to_email=None,
            area_path=item.area_path,
            iteration_path="",
            target_date=item.target_date,
            risk_level=item.risk_level,
            tags=list(item.tags),
            custom_fields={"changed_date": snapshot.ado_data_as_of.isoformat()},
            revisions=[],
            comments=[],
            fetched_at=snapshot.ado_data_as_of,
        )
        for item in snapshot.items
    )


def _select_primary_edition(program_document: dict[str, object], archive_root: Path) -> str | None:
    communication_plan_entries = tuple(load_communication_plan_entries(program_document))
    if not archive_root.exists():
        return communication_plan_entries[0].edition if communication_plan_entries else None

    confirmed_editions = {
        edition_dir.name
        for edition_dir in archive_root.iterdir()
        if edition_dir.is_dir()
        and find_latest_confirmed_entry(read_archive_index(edition_dir.name, archive_root=archive_root)) is not None
    }
    if not confirmed_editions:
        return communication_plan_entries[0].edition if communication_plan_entries else None

    for entry in communication_plan_entries:
        if entry.edition in confirmed_editions:
            return entry.edition

    return min(confirmed_editions, key=lambda edition_name: (_edition_priority(edition_name), edition_name))


def _latest_confirmed_label(program: FleetProgramSummary) -> str:
    if program.latest_issue_number is None or program.latest_confirmed_at is None:
        return "none yet"
    return f"issue {program.latest_issue_number:03d} on {program.latest_confirmed_at.date().isoformat()}"


def _find_communication_plan_entry(program_document: dict[str, object], edition_name: str) -> CommunicationPlanEntry | None:
    for entry in load_communication_plan_entries(program_document):
        if entry.edition == edition_name:
            return entry
    return None


def _load_cross_program_dependencies(programs_root: Path) -> tuple[Dependency, ...]:
    dependencies: list[Dependency] = []
    for program_dir in sorted(programs_root.iterdir(), key=lambda entry: entry.name.lower()):
        if not program_dir.is_dir() or not (program_dir / "program.yaml").exists():
            continue
        try:
            loaded_dependencies = project_dependencies(
                load_program_facts(program_dir.name, programs_root=programs_root)
            )
        except ConfigError:
            continue
        dependencies.extend(
            dependency
            for dependency in loaded_dependencies
            if isinstance(dependency, Dependency) and dependency.from_program_id != dependency.to_program_id
        )
    return tuple(dependencies)


def _build_cross_program_dependency_index(
    dependencies: tuple[Dependency, ...],
) -> dict[str, tuple[FleetDependencySummary, ...]]:
    index: dict[str, list[FleetDependencySummary]] = {}
    for dependency in dependencies:
        outbound = FleetDependencySummary(
            direction="outbound",
            counterpart_program_id=dependency.to_program_id,
            dependency_type=dependency.dependency_type.value,
            status=dependency.status.value,
            source_label=dependency_source_label(dependency),
            target_label=dependency_target_label(dependency),
            impact=dependency_impact_text(dependency),
        )
        inbound = FleetDependencySummary(
            direction="inbound",
            counterpart_program_id=dependency.from_program_id,
            dependency_type=dependency.dependency_type.value,
            status=dependency.status.value,
            source_label=dependency_source_label(dependency),
            target_label=dependency_target_label(dependency),
            impact=dependency_impact_text(dependency),
        )
        index.setdefault(dependency.from_program_id, []).append(outbound)
        index.setdefault(dependency.to_program_id, []).append(inbound)

    return {
        program_id: tuple(
            sorted(
                entries,
                key=lambda entry: (0 if entry.direction == "outbound" else 1, entry.counterpart_program_id, entry.target_label, entry.source_label),
            )
        )
        for program_id, entries in index.items()
    }


def _build_dependency_heat_index(
    dependencies: tuple[Dependency, ...],
    *,
    max_hops: int = 3,
) -> dict[str, tuple[FleetDependencyChainSummary, ...]]:
    adjacency: dict[str, tuple[Dependency, ...]] = {}
    for dependency in dependencies:
        adjacency.setdefault(dependency.from_program_id, ())
        adjacency[dependency.from_program_id] = adjacency[dependency.from_program_id] + (dependency,)

    index: dict[str, list[FleetDependencyChainSummary]] = {}
    for source_program_id in sorted(adjacency):
        chains = _build_program_dependency_heat_chains(
            source_program_id,
            adjacency=adjacency,
            max_hops=max_hops,
        )
        if chains:
            index[source_program_id] = list(chains)
    return {program_id: tuple(chains) for program_id, chains in index.items()}


def _build_program_dependency_heat_chains(
    source_program_id: str,
    *,
    adjacency: dict[str, tuple[Dependency, ...]],
    max_hops: int,
) -> tuple[FleetDependencyChainSummary, ...]:
    seen: set[tuple[str, ...]] = set()
    chains: list[FleetDependencyChainSummary] = []

    def _walk(current_program_id: str, path: tuple[Dependency, ...], visited_program_ids: frozenset[str]) -> None:
        if len(path) >= 2 and any(dependency.status.value == "broken" for dependency in path):
            program_path = (source_program_id, *(dependency.to_program_id for dependency in path))
            if program_path not in seen:
                seen.add(program_path)
                chains.append(
                    FleetDependencyChainSummary(
                        program_path=program_path,
                        route=" => ".join(
                            f"{dependency_source_label(dependency)} -> {dependency_target_label(dependency)}"
                            for dependency in path
                        ),
                        hop_count=len(path),
                        broken_hop_count=sum(1 for dependency in path if dependency.status.value == "broken"),
                    )
                )
        if len(path) >= max_hops:
            return
        for dependency in adjacency.get(current_program_id, ()): 
            if dependency.to_program_id in visited_program_ids:
                continue
            _walk(
                dependency.to_program_id,
                path + (dependency,),
                visited_program_ids | {dependency.to_program_id},
            )

    _walk(source_program_id, (), frozenset({source_program_id}))
    return tuple(
        sorted(
            chains,
            key=lambda chain: (-chain.broken_hop_count, -chain.hop_count, chain.program_path),
        )
    )


def _aggregate_trend(
    *,
    current: RiskLevel,
    prior: RiskLevel | None,
) -> Literal["improving", "stable", "worsening"]:
    if prior is None:
        return "stable"
    current_rank = _risk_rank(current)
    prior_rank = _risk_rank(prior)
    if current_rank < prior_rank:
        return "improving"
    if current_rank > prior_rank:
        return "worsening"
    return "stable"


def _aggregate_trend_detail(trends: dict[tuple[str, str], ScorecardTrend]) -> str | None:
    worsening = sum(1 for trend in trends.values() if getattr(trend, "direction", None) == "worsening")
    improving = sum(1 for trend in trends.values() if getattr(trend, "direction", None) == "improving")
    if worsening == 0 and improving == 0:
        return None
    return f"{worsening} worsening, {improving} improving dimensions"


def _compute_staleness(
    edition_name: str,
    latest_confirmed_at: datetime,
    as_of: datetime,
    *,
    cadence_label: str | None = None,
) -> tuple[bool, str]:
    cadence = _cadence_window(cadence_label) if cadence_label is not None else None
    if cadence is None:
        cadence = _infer_cadence(edition_name)
    threshold = _DEFAULT_STALE_WINDOW
    if cadence is not None:
        threshold = max(_DEFAULT_STALE_WINDOW, cadence * 1.5)
    elapsed = as_of - latest_confirmed_at
    if elapsed <= threshold:
        return False, "current"
    overdue_days = max(1, int((elapsed - threshold).total_seconds() // timedelta(days=1).total_seconds()))
    return True, f"stale by {overdue_days} day{'s' if overdue_days != 1 else ''}"


def _infer_cadence(edition_name: str) -> timedelta | None:
    normalized = edition_name.strip().lower()
    for label, window in _CADENCE_WINDOWS.items():
        if label in normalized:
            return window
    return None


def _cadence_window(cadence_label: str | None) -> timedelta | None:
    if cadence_label is None:
        return None
    return _CADENCE_WINDOWS.get(cadence_label.strip().lower())


def _edition_priority(edition_name: str) -> int:
    normalized = edition_name.strip().lower()
    if "weekly" in normalized:
        return 0
    if "daily" in normalized:
        return 1
    if "deck" in normalized:
        return 2
    if "monthly" in normalized:
        return 3
    if "quarterly" in normalized:
        return 4
    return 99


def _previous_issue_number(scorecard_history: tuple[dict[str, object], ...], latest_issue_number: int) -> int | None:
    prior_issue_numbers = sorted(
        {
            issue_number
            for issue_number in (_issue_number(entry.get("issue_number")) for entry in scorecard_history)
            if issue_number is not None and issue_number < latest_issue_number
        }
    )
    if not prior_issue_numbers:
        return None
    return prior_issue_numbers[-1]


def _overall_risk_for_issue(scorecard_history: tuple[dict[str, object], ...], issue_number: int | None) -> RiskLevel | None:
    if issue_number is None:
        return None
    risks = [
        RiskLevel.from_string(str(entry.get("risk") or ""))
        for entry in scorecard_history
        if _issue_number(entry.get("issue_number")) == issue_number and str(entry.get("risk") or "").strip()
    ]
    if not risks:
        return None
    return max(risks, key=_risk_rank)


def _issue_number(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _risk_rank(level: RiskLevel) -> int:
    return {
        RiskLevel.UNKNOWN: 0,
        RiskLevel.DONE: 1,
        RiskLevel.LOW: 2,
        RiskLevel.MEDIUM: 3,
        RiskLevel.HIGH: 4,
    }[level]


def _render_dependency_line(dependency: FleetDependencySummary) -> str:
    return (
        f"{dependency.direction} {dependency.counterpart_program_id} | {dependency.status.upper()} {dependency.dependency_type} | "
        f"{dependency.source_label} -> {dependency.target_label} | {dependency.impact}"
    )


def _render_dependency_heat_line(chain: FleetDependencyChainSummary) -> str:
    return (
        f"{' -> '.join(chain.program_path)} | {chain.hop_count} hop(s), {chain.broken_hop_count} broken | {chain.route}"
    )


def _render_dependency_health_summary(summary: FleetDependencyHealthSummary) -> str:
    return (
        f"{summary.total_count} linked, {summary.outbound_count} outbound, "
        f"{summary.inbound_count} inbound, {summary.broken_count} broken"
    )


def _detail_suffix(detail: str | None) -> str:
    return f" ({detail})" if detail else ""


def _parse_program_ids(raw_value: str | None) -> tuple[str, ...] | None:
    if raw_value is None:
        return None
    parsed = tuple(part.strip() for part in raw_value.split(",") if part.strip())
    return parsed or None