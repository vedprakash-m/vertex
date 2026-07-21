from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable

from src.core.claim_tracker import ClaimAssessment
from src.core.coverage_gap import CoverageGap, coverage_gap_confidence_label
from src.core.context_proposal_review import ContextProposalReviewRow
from src.core.dependency_scout import DependencyProposal, dependency_proposal_confidence_label
from src.core.forecast_engine import ETAForecast
from src.core.issue_projection import IssueProjection, issue_projection_confidence_label, issue_projection_source_label
from src.core.models import Confidence, WorkItem
from src.core.models_v2 import Signal
from src.core.narrative_store import REMOVED_SECTION_MARKER
from src.core.quality_gates import QualityGateReport
from src.core.store_factory import build_trajectory_store_for_program_id
from src.core.vitality_scorer import VitalitySummary
from src.core.view_models import WorkstreamData


@dataclass(frozen=True, slots=True)
class ReadinessAssessment:
    score: int
    quality_gate_pass_rate: int
    quality_gate_passed: int
    quality_gate_total: int
    unreviewed_signal_count: int
    missing_narrative_count: int
    missing_override_count: int
    coverage_gap_count: int
    written_narrative_count: int
    total_narrative_count: int
    set_override_count: int
    total_override_count: int

    def to_payload(self) -> dict[str, int | str]:
        return {
            "score": self.score,
            "quality_gate_pass_rate": self.quality_gate_pass_rate,
            "quality_gate_passed": self.quality_gate_passed,
            "quality_gate_total": self.quality_gate_total,
            "unreviewed_signal_count": self.unreviewed_signal_count,
            "missing_narrative_count": self.missing_narrative_count,
            "missing_override_count": self.missing_override_count,
            "coverage_gap_count": self.coverage_gap_count,
            "written_narrative_count": self.written_narrative_count,
            "total_narrative_count": self.total_narrative_count,
            "set_override_count": self.set_override_count,
            "total_override_count": self.total_override_count,
            "summary": self.summary,
        }

    @property
    def summary(self) -> str:
        details: list[str] = []
        if self.missing_narrative_count:
            details.append(_pluralize(self.missing_narrative_count, "narrative", suffix=" missing"))
        if self.unreviewed_signal_count:
            details.append(_pluralize(self.unreviewed_signal_count, "signal", suffix=" unreviewed"))
        if self.missing_override_count:
            details.append(_pluralize(self.missing_override_count, "override", suffix=" missing"))
        if self.coverage_gap_count:
            details.append(_pluralize(self.coverage_gap_count, "coverage gap"))
        if not details:
            details.append("no outstanding blockers")
        return f"Draft readiness: {self.score}% — {', '.join(details)}."


@dataclass(frozen=True, slots=True)
class StaleNarrativeFinding:
    section_id: str
    section_title: str
    narrative_path: str
    narrative_last_modified: datetime
    work_item_id: int
    work_item_title: str
    eta_changed_on: date
    confidence: Confidence = Confidence.NONE

    @property
    def detail(self) -> str:
        return (
            f"{self.narrative_path} last edited {_format_calendar_date(self.narrative_last_modified)}, "
            f"but WI:{self.work_item_id} ETA changed {_format_calendar_date(self.eta_changed_on)}"
        )

    @property
    def detail_with_confidence(self) -> str:
        return f"{self.detail} ({stale_narrative_confidence_label(self)})"


def stale_narrative_confidence_label(finding: StaleNarrativeFinding) -> str:
    return f"{finding.confidence.value.lower()} confidence"


@dataclass(frozen=True, slots=True)
class CorrelatedTriageItem:
    work_item_id: int
    work_item_title: str
    work_item_state: str
    details: tuple[str, ...]
    confidence: Confidence = Confidence.NONE


def correlated_triage_confidence_label(item: CorrelatedTriageItem) -> str:
    return f"{item.confidence.value.lower()} confidence"


@dataclass(frozen=True, slots=True)
class IncidentLearning:
    summary: str
    confidence: Confidence = Confidence.NONE

    @property
    def summary_with_confidence(self) -> str:
        return f"{self.summary} ({incident_learning_confidence_label(self)})"


def incident_learning_confidence_label(item: IncidentLearning) -> str:
    return f"{item.confidence.value.lower()} confidence"


@dataclass(frozen=True, slots=True)
class TriageReport:
    edition_name: str
    issue_number: int
    program_id: str | None
    readiness: ReadinessAssessment
    blockers: tuple[str, ...]
    needs_attention: tuple[str, ...]
    milestones: tuple[str, ...]
    risks: tuple[str, ...]
    actions: tuple[str, ...]
    decisions: tuple[str, ...]
    assumptions: tuple[str, ...]
    cross_program_cascades: tuple[str, ...]
    active_issues: tuple[IssueProjection, ...]
    coverage_gaps: tuple[CoverageGap, ...]
    ready: tuple[str, ...]
    coverage_gap_window_days: int
    integration_diagnostics: tuple[str, ...] = ()
    telemetry: tuple[str, ...] = ()
    scorecard_composition: tuple[str, ...] = ()
    section_roster: tuple[str, ...] = ()
    correlated_items: tuple[CorrelatedTriageItem, ...] = ()
    vitality_enabled: bool = False
    vitality_summary: VitalitySummary | None = None
    stale_narratives: tuple[StaleNarrativeFinding, ...] = ()
    stale_claims: tuple[ClaimAssessment, ...] = ()
    open_decision_ask_count: int = 0
    contradictions: tuple[str, ...] = ()
    decision_debt: tuple[str, ...] = ()
    dependency_proposals: tuple[DependencyProposal, ...] = ()
    incident_learnings: tuple[IncidentLearning, ...] = ()
    context_proposals: tuple[ContextProposalReviewRow, ...] = ()
    # WI-3.3: Phase-3 signal quality counters (populated when program_facts loaded)
    auto_approved_signal_count: int = 0
    provisional_signal_count: int = 0
    material_conflict_count: int = 0
    # WI-2.6: Unresolved entity_refs count (facts referencing unknown entities)
    unresolved_entity_ref_count: int = 0

    @property
    def exit_code(self) -> int:
        if self.blockers:
            return 3
        if self.needs_attention or self.coverage_gaps:
            return 2
        return 0


def is_missing_narrative_content(content: str) -> bool:
    stripped = content.strip()
    return not stripped or stripped.startswith(REMOVED_SECTION_MARKER) or "<!-- SCAFFOLD -->" in stripped


def build_readiness_assessment(
    *,
    quality_gate_report: QualityGateReport,
    unreviewed_signal_count: int,
    missing_narrative_count: int,
    total_narrative_count: int,
    missing_override_count: int,
    total_override_count: int,
    coverage_gap_count: int,
) -> ReadinessAssessment:
    gate_total = len(quality_gate_report.results)
    gate_passed = sum(1 for result in quality_gate_report.results if result.passed)
    quality_gate_pass_rate = 100 if gate_total == 0 else round((gate_passed / gate_total) * 100)
    written_narrative_count = max(0, total_narrative_count - missing_narrative_count)
    set_override_count = max(0, total_override_count - missing_override_count)
    narrative_score = 100 if total_narrative_count == 0 else round((written_narrative_count / total_narrative_count) * 100)
    override_score = 100 if total_override_count == 0 else round((set_override_count / total_override_count) * 100)
    signal_score = max(0, 100 - min(100, unreviewed_signal_count * 10))
    coverage_score = max(0, 100 - min(100, coverage_gap_count * 10))
    score = round((quality_gate_pass_rate + narrative_score + override_score + signal_score + coverage_score) / 5)
    return ReadinessAssessment(
        score=score,
        quality_gate_pass_rate=quality_gate_pass_rate,
        quality_gate_passed=gate_passed,
        quality_gate_total=gate_total,
        unreviewed_signal_count=unreviewed_signal_count,
        missing_narrative_count=missing_narrative_count,
        missing_override_count=missing_override_count,
        coverage_gap_count=coverage_gap_count,
        written_narrative_count=written_narrative_count,
        total_narrative_count=total_narrative_count,
        set_override_count=set_override_count,
        total_override_count=total_override_count,
    )


def finalize_triage_report(
    *,
    edition_name: str,
    issue_number: int,
    program_id: str | None,
    quality_gate_report: QualityGateReport,
    unreviewed_signals: tuple[Signal, ...],
    milestone_summaries: tuple[str, ...] = (),
    milestone_attention: tuple[str, ...] = (),
    risk_summaries: tuple[str, ...] = (),
    risk_attention: tuple[str, ...] = (),
    action_summaries: tuple[str, ...] = (),
    action_attention: tuple[str, ...] = (),
    decision_summaries: tuple[str, ...] = (),
    decision_attention: tuple[str, ...] = (),
    assumption_summaries: tuple[str, ...] = (),
    assumption_attention: tuple[str, ...] = (),
    integration_diagnostics: tuple[str, ...] = (),
    telemetry_summaries: tuple[str, ...] = (),
    scorecard_composition: tuple[str, ...] = (),
    section_roster: tuple[str, ...] = (),
    cross_program_cascades: tuple[str, ...] = (),
    active_issues: tuple[IssueProjection, ...] = (),
    correlated_items: tuple[CorrelatedTriageItem, ...] = (),
    missing_narrative_ids: tuple[str, ...],
    total_narrative_count: int,
    missing_override_names: tuple[str, ...],
    total_override_count: int,
    coverage_gaps: tuple[CoverageGap, ...],
    eta_forecasts: dict[int, ETAForecast],
    items: tuple[WorkItem, ...],
    coverage_gap_window_days: int,
    vitality_enabled: bool | None = None,
    vitality_summary: VitalitySummary | None = None,
    stale_narratives: tuple[StaleNarrativeFinding, ...] = (),
    stale_claims: tuple[ClaimAssessment, ...] = (),
    open_decision_ask_count: int = 0,
    contradictions: tuple[str, ...] = (),
    decision_debt: tuple[str, ...] = (),
    dependency_proposals: tuple[DependencyProposal, ...] = (),
    incident_learnings: tuple[IncidentLearning, ...] = (),
    context_proposals: tuple[ContextProposalReviewRow, ...] = (),
    # WI-3.3: Phase-3 signal quality counters
    auto_approved_signal_count: int = 0,
    provisional_signal_count: int = 0,
    material_conflict_count: int = 0,
    # WI-2.6: Unresolved entity_refs count
    unresolved_entity_ref_count: int = 0,
) -> TriageReport:
    readiness = build_readiness_assessment(
        quality_gate_report=quality_gate_report,
        unreviewed_signal_count=len(unreviewed_signals),
        missing_narrative_count=len(missing_narrative_ids),
        total_narrative_count=total_narrative_count,
        missing_override_count=len(missing_override_names),
        total_override_count=total_override_count,
        coverage_gap_count=len(coverage_gaps),
    )
    blockers = tuple(f"{result.gate_id}: {result.message}" for result in quality_gate_report.failing_results)
    needs_attention: list[str] = []
    if unreviewed_signals:
        hint = f" (vertex signals review --program {program_id})" if program_id is not None else ""
        needs_attention.append(f"{_pluralize(len(unreviewed_signals), 'unreviewed signal')}{hint}")
        needs_attention.extend(_unreviewed_signal_attention_lines(unreviewed_signals))
    if contradictions:
        needs_attention.append(f"{_pluralize(len(contradictions), 'active contradiction')} (see CONTRADICTIONS)")
    if decision_debt:
        needs_attention.append(f"{_pluralize(len(decision_debt), 'aged decision ask')} (see DECISION DEBT)")
    if dependency_proposals:
        needs_attention.append(
            f"{_pluralize(len(dependency_proposals), 'pending dependency proposal')} (see DEPENDENCY PROPOSALS)"
        )
    if incident_learnings:
        needs_attention.append(
            f"{_pluralize(len(incident_learnings), 'recent incident learning')} (see INCIDENT LEARNINGS)"
        )
    if context_proposals:
        needs_attention.append(
            f"{_pluralize(len(context_proposals), 'pending context revision')} (see CONTEXT REVISIONS)"
        )
    needs_attention.extend(milestone_attention)
    needs_attention.extend(risk_attention)
    needs_attention.extend(action_attention)
    needs_attention.extend(decision_attention)
    needs_attention.extend(assumption_attention)
    if integration_diagnostics:
        needs_attention.append(
            f"{_pluralize(len(integration_diagnostics), 'integration diagnostic')} (see INTEGRATION DIAGNOSTICS)"
        )
    if cross_program_cascades:
        needs_attention.append(
            f"{_pluralize(len(cross_program_cascades), 'cross-program dependency cascade warning')} (see CROSS-PROGRAM CASCADES)"
        )

    forecast_lines = _forecast_attention_lines(eta_forecasts, items)
    needs_attention.extend(forecast_lines)

    if stale_narratives:
        needs_attention.extend(_stale_narrative_attention_lines(stale_narratives))

    if stale_claims:
        needs_attention.extend(_claim_attention_lines(stale_claims))
    if open_decision_ask_count:
        needs_attention.append(_pluralize(open_decision_ask_count, "open decision ask"))

    if missing_narrative_ids:
        needs_attention.append(f"{_pluralize(len(missing_narrative_ids), 'workstream narrative')} not written")

    ready = (
        f"{readiness.written_narrative_count}/{readiness.total_narrative_count} workstream narratives written",
        f"{readiness.set_override_count}/{readiness.total_override_count} risk overrides set",
        _quality_gate_summary(quality_gate_report),
    )
    return TriageReport(
        edition_name=edition_name,
        issue_number=issue_number,
        program_id=program_id,
        readiness=readiness,
        blockers=blockers,
        needs_attention=tuple(needs_attention),
        milestones=milestone_summaries,
        risks=risk_summaries,
        actions=action_summaries,
        decisions=decision_summaries,
        assumptions=assumption_summaries,
        integration_diagnostics=integration_diagnostics,
        telemetry=telemetry_summaries,
        scorecard_composition=scorecard_composition,
        section_roster=section_roster,
        cross_program_cascades=cross_program_cascades,
        active_issues=active_issues,
        correlated_items=correlated_items,
        coverage_gaps=coverage_gaps,
        ready=ready,
        coverage_gap_window_days=coverage_gap_window_days,
        vitality_enabled=(vitality_summary is not None if vitality_enabled is None else vitality_enabled),
        vitality_summary=vitality_summary,
        stale_narratives=stale_narratives,
        stale_claims=stale_claims,
        open_decision_ask_count=open_decision_ask_count,
        contradictions=contradictions,
        decision_debt=decision_debt,
        dependency_proposals=dependency_proposals,
        incident_learnings=incident_learnings,
        context_proposals=context_proposals,
        auto_approved_signal_count=auto_approved_signal_count,
        provisional_signal_count=provisional_signal_count,
        material_conflict_count=material_conflict_count,
        unresolved_entity_ref_count=unresolved_entity_ref_count,
    )


def _unreviewed_signal_attention_lines(
    unreviewed_signals: tuple[Signal, ...],
    *,
    limit: int = 3,
) -> tuple[str, ...]:
    lines: list[str] = []
    for signal in unreviewed_signals[:limit]:
        entity_ref = _primary_entity_ref(signal)
        confidence_label = f"{signal.confidence.value} confidence"
        if entity_ref is None:
            lines.append(f"Priority review: {signal.source} | {confidence_label} | {signal.text.strip()}")
            continue
        lines.append(f"Priority review: {signal.source} | {entity_ref} | {confidence_label} | {signal.text.strip()}")
    return tuple(lines)


def _primary_entity_ref(signal: Signal) -> str | None:
    for entity_ref in signal.entity_refs:
        candidate = str(entity_ref).strip()
        if candidate:
            return candidate
    return None


def detect_stale_narratives(
    *,
    program_id: str | None,
    workstream_data: tuple[WorkstreamData, ...],
    narratives_dir: Path,
    programs_root: Path,
) -> tuple[StaleNarrativeFinding, ...]:
    if program_id is None:
        return ()

    findings: list[StaleNarrativeFinding] = []
    for workstream in workstream_data:
        if workstream.narrative_empty or not workstream.items or not workstream.edit_path:
            continue
        narrative_path = narratives_dir / Path(workstream.edit_path).name
        if not narrative_path.exists():
            continue
        latest_eta_change = _latest_eta_change(program_id, workstream.items, programs_root)
        if latest_eta_change is None:
            continue
        narrative_last_modified = datetime.fromtimestamp(narrative_path.stat().st_mtime, tz=timezone.utc)
        if narrative_last_modified.date() >= latest_eta_change[0]:
            continue
        change_date, item = latest_eta_change
        findings.append(
            StaleNarrativeFinding(
                section_id=workstream.section_id,
                section_title=workstream.title,
                narrative_path=workstream.edit_path,
                narrative_last_modified=narrative_last_modified,
                work_item_id=item.id,
                work_item_title=item.title,
                eta_changed_on=change_date,
                confidence=Confidence.HIGH,
            )
        )
    return tuple(findings)


def render_triage_report(report: TriageReport) -> str:
    lines = [
        f"Triage: {report.edition_name} | Issue #{report.issue_number:03d} | Draft readiness: {report.readiness.score}%",
        "",
        "BLOCKERS:",
        *_render_lines(report.blockers),
        "",
        "NEEDS ATTENTION:",
        *_render_lines(report.needs_attention),
        "",
        "CONTRADICTIONS:",
        *_render_lines(report.contradictions),
        "",
        "DECISION DEBT:",
        *_render_lines(report.decision_debt),
        "",
        "DEPENDENCY PROPOSALS:",
        *_render_dependency_proposals(report.dependency_proposals),
        "",
        "INCIDENT LEARNINGS:",
        *_render_incident_learnings(report.incident_learnings),
        "",
        "CONTEXT REVISIONS:",
        *_render_context_proposals(report.context_proposals),
        "",
        "MILESTONE HEALTH:",
        *_render_lines(report.milestones),
        "",
        "RISK REGISTER:",
        *_render_lines(report.risks),
        "",
        "ACTIONS:",
        *_render_lines(report.actions),
        "",
        "DECISIONS:",
        *_render_lines(report.decisions),
        "",
        "ASSUMPTIONS:",
        *_render_lines(report.assumptions),
        "",
        "INTEGRATION DIAGNOSTICS:",
        *_render_lines(report.integration_diagnostics),
        "",
        "TELEMETRY:",
        *_render_lines(report.telemetry),
        "",
        "SCORECARD COMPOSITION:",
        *_render_lines(report.scorecard_composition),
        "",
        "SECTION ROSTER:",
        *_render_lines(report.section_roster),
        "",
        "CROSS-PROGRAM CASCADES:",
        *_render_lines(report.cross_program_cascades),
        "",
        "ACTIVE ISSUES:",
        *_render_issue_projections(report.active_issues),
        "",
        "CORRELATED ITEMS:",
        *_render_correlated_items(report.correlated_items),
        "",
        "COVERAGE GAPS:",
    ]
    if report.coverage_gaps:
        lines.append(
            f"  - {_pluralize(len(report.coverage_gaps), 'active item')} with no approved signals or narrative mention in {report.coverage_gap_window_days} days"
        )
        for gap in report.coverage_gaps:
            lines.append(
                f"  - WI:{gap.work_item_id} \"{gap.title}\" ({gap.state}; {coverage_gap_confidence_label(gap)})"
            )
    else:
        lines.append("  - None")
    if report.vitality_enabled:
        lines.extend(("", "ADO VITALITY:", *_render_vitality_lines(report.vitality_summary)))
    # WI-3.3: Phase-3 signal quality summary
    any_signal_data = (
        report.auto_approved_signal_count > 0
        or report.provisional_signal_count > 0
        or report.material_conflict_count > 0
        or report.unresolved_entity_ref_count > 0
    )
    if any_signal_data:
        conflict_note = f" | {report.material_conflict_count} material conflict(s)" if report.material_conflict_count > 0 else ""
        unresolved_note = f" | {report.unresolved_entity_ref_count} unresolved entity ref(s)" if report.unresolved_entity_ref_count > 0 else ""
        lines.extend((
            "",
            "SIGNAL QUALITY:",
            f"  - auto_approved={report.auto_approved_signal_count} | provisional={report.provisional_signal_count}{conflict_note}{unresolved_note}",
        ))
    lines.extend(("", "READY:", *_render_lines(report.ready), "", report.readiness.summary))
    return "\n".join(lines)


def _render_lines(lines: Iterable[str]) -> tuple[str, ...]:
    rendered = tuple(f"  - {line}" for line in lines if line)
    return rendered or ("  - None",)


def _render_dependency_proposals(proposals: tuple[DependencyProposal, ...]) -> tuple[str, ...]:
    rendered = tuple(
        (
            f"  - {proposal.id} | {proposal.from_workstream_id}:{proposal.from_item_id} -> "
            f"{proposal.to_workstream_id}:{proposal.to_item_id} | {proposal.detection_method} | "
            f"{proposal.occurrence_count} signal(s) | {dependency_proposal_confidence_label(proposal)}"
        )
        for proposal in proposals
    )
    return rendered or ("  - None",)


def _render_incident_learnings(items: tuple[IncidentLearning, ...]) -> tuple[str, ...]:
    rendered = tuple(f"  - {item.summary_with_confidence}" for item in items)
    return rendered or ("  - None",)


def _render_context_proposals(rows: tuple[ContextProposalReviewRow, ...]) -> tuple[str, ...]:
    rendered = tuple(
        (
            f"  - {row.proposal_id} | {row.target} | proposed={row.proposed_value!r} | "
            f"current_hash={row.current_hash_label} | evidence={row.evidence} | "
            f"{row.conflict_state} | Next: {row.next_command}"
        )
        for row in rows
    )
    return rendered or ("  - None",)


def _render_correlated_items(items: tuple[CorrelatedTriageItem, ...]) -> tuple[str, ...]:
    if not items:
        return ("  - None",)

    lines: list[str] = []
    for item in items:
        lines.append(
            f"  - WI:{item.work_item_id} \"{item.work_item_title}\" ({item.work_item_state}; {correlated_triage_confidence_label(item)})"
        )
        for detail in item.details:
            lines.append(f"    - {detail}")
    return tuple(lines)


def _render_issue_projections(items: tuple[IssueProjection, ...]) -> tuple[str, ...]:
    if not items:
        return ("  - None",)

    lines: list[str] = []
    for item in items:
        source_label = issue_projection_source_label(item)
        confidence_label = issue_projection_confidence_label(item)
        lines.append(f"  - {item.severity.upper()} | {source_label} | {confidence_label} | {item.summary}")
        details: list[str] = []
        if item.owner_alias is not None:
            details.append(f"owner {item.owner_alias}")
        if item.workstream_id is not None:
            details.append(f"workstream {item.workstream_id}")
        if item.linked_entity_ids:
            details.append(f"linked {', '.join(item.linked_entity_ids)}")
        if item.ado_url is not None:
            details.append(f"ado {item.ado_url}")
        if details:
            lines.append(f"    - {' | '.join(details)}")
    return tuple(lines)


def _forecast_attention_lines(
    eta_forecasts: dict[int, ETAForecast],
    items: tuple[WorkItem, ...],
) -> tuple[str, ...]:
    item_lookup = {item.id: item for item in items}
    lines: list[str] = []
    candidates = [forecast for forecast in eta_forecasts.values() if forecast.annotation is not None]
    if not candidates:
        return ()
    lines.append(f"{_pluralize(len(candidates), 'item')} with ETA drift signals")
    for forecast in sorted(candidates, key=lambda entry: entry.work_item_id):
        item = item_lookup.get(forecast.work_item_id)
        if item is None:
            continue
        eta_label = forecast.ado_target_date.strftime('%b %d') if forecast.ado_target_date is not None else "n/a"
        lines.append(f"WI:{item.id} \"{item.title}\" — ETA {eta_label} ({forecast.display_annotation})")
    return tuple(lines)


def _claim_attention_lines(stale_claims: tuple[ClaimAssessment, ...]) -> tuple[str, ...]:
    lines: list[str] = []
    for assessment in stale_claims:
        prefix = "Contradicted claim" if assessment.effective_status == "contradicted" else "Stale claim"
        detail = assessment.reason or "Claim no longer matches the current program state."
        lines.append(
            f"{prefix} from issue #{assessment.claim.issue_number}: \"{assessment.claim.text}\" — {detail} ({assessment.confidence.value.lower()} confidence)"
        )
    return tuple(lines)


def _stale_narrative_attention_lines(stale_narratives: tuple[StaleNarrativeFinding, ...]) -> tuple[str, ...]:
    return tuple(
        f"Stale narrative for {finding.section_title}: {finding.detail_with_confidence}."
        for finding in stale_narratives
    )


def _render_vitality_lines(summary: VitalitySummary | None) -> tuple[str, ...]:
    if summary is None or summary.total_items == 0:
        return ("  - None",)
    stale_owners = ", ".join(summary.stale_owner_aliases) if summary.stale_owner_aliases else "none"
    return (
        f"  - {summary.updated_this_week}/{summary.total_items} items updated this week ({summary.updated_this_week_percentage}%)",
        f"  - Freshness average: {summary.freshness_average_days:.1f} days since last meaningful update",
        f"  - Owners with stale items: {stale_owners}",
    )


def _quality_gate_summary(quality_gate_report: QualityGateReport) -> str:
    if quality_gate_report.passed:
        return "All quality gates passing"
    failing_gate_ids = ", ".join(result.gate_id for result in quality_gate_report.failing_results)
    return f"All quality gates passing except {failing_gate_ids}"


def _latest_eta_change(
    program_id: str,
    items: tuple[WorkItem, ...],
    programs_root: Path,
) -> tuple[date, WorkItem] | None:
    latest: tuple[date, WorkItem] | None = None
    trajectory_store = build_trajectory_store_for_program_id(program_id, programs_root=programs_root)
    for item in items:
        trajectory = trajectory_store.read(program_id, item.id)
        for previous, current in zip(trajectory, trajectory[1:]):
            if previous.target_date == current.target_date:
                continue
            candidate = (current.date, item)
            if latest is None or candidate[0] > latest[0] or (candidate[0] == latest[0] and candidate[1].id < latest[1].id):
                latest = candidate
    return latest


def _format_calendar_date(value: date | datetime) -> str:
    actual_date = value.date() if isinstance(value, datetime) else value
    return f"{actual_date.strftime('%b')} {actual_date.day}"


def _pluralize(count: int, noun: str, *, suffix: str = "") -> str:
    ending = "" if count == 1 else "s"
    return f"{count} {noun}{ending}{suffix}"
