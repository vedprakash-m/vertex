from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path

from src.commands.report_top_items import _risk_from_top_item_type
from src.core.assumption_tracker import check_validation_due
from src.core.charter import normalize_charter_values
from src.core.claim_tracker import load_decision_asks, load_latest_claim_statuses, load_open_decision_asks
from src.core.decision_register import assess_proposed_decision_staleness
from src.core.deck_renderer import DeckAskRow, DeckAssumptionRow, DeckChangeRow, DeckDataRow, DeckDecisionRow, DeckDependencyProposalRow, DeckHealthRow, DeckIssueRow, DeckMilestoneRow, DeckRenderContext, DeckRiskRow, DeckTopRiskRow
from src.core.dependency_scout import DependencyProposalStatus, load_dependency_proposals
from src.core.forecast_engine import ETAForecast
from src.core.issue_projection import IssueProjection, issue_projection_confidence_label, issue_projection_source_label
from src.core.jinja_filters import delta_label
from src.core.milestone_engine import (
    describe_milestone_schedule_variance,
    load_milestone_completion_date_history_map,
    load_milestone_target_date_history_map,
    summarize_milestone_completion_date_history,
    summarize_milestone_target_date_history,
)
from src.core.models import DeltaKind, DeltaSet, DimensionRisk, ItemDelta, RiskLevel, ScorecardDelta, WorkItem
from src.core.models_v2 import Assumption, AssumptionStatus, DecisionEntry, DecisionStatus, Milestone, MilestoneAssessment, MilestoneStatus, RiskEntry, RiskStatus
from src.core.program_reality import ProgramReality
from src.core.risk_register_engine import assess_risk_staleness, compute_risk_score
from src.core.signal_review import signal_is_approved_for_evidence
from src.core.store_factory import build_signal_store_for_program_id, build_trajectory_store_for_program_id
from src.core.telemetry_summary import build_program_telemetry_summary
from src.core.trajectory import read_trajectory
from src.core.view_models import MilestoneSummaryRow, Top3Item


def _format_deck_issue_date(value: datetime) -> str:
    return f"{value.strftime('%B')} {value.day}, {value.year}"


def _format_deck_generated_at(value: datetime) -> str:
    return value.strftime("%b %d %Y, %H:%M UTC")


def _truncate_deck_words(text: str, limit: int) -> str:
    words = text.split()
    if len(words) <= limit:
        return " ".join(words)
    return " ".join(words[:limit]) + "..."


def _format_deck_item_delta(delta: ItemDelta) -> str:
    if delta.kind in {DeltaKind.RISK_UP, DeltaKind.RISK_DOWN}:
        return delta_label(delta.kind, delta.old_risk, delta.new_risk)
    if delta.kind == DeltaKind.ETA_CHANGED:
        return delta_label(delta.kind, delta.old_eta, delta.new_eta)
    return delta_label(delta.kind)


def _format_deck_risk_change(old_risk: RiskLevel, new_risk: RiskLevel) -> str:
    return f"{old_risk.value.title()} -> {new_risk.value.title()}"


def _count_label(count: int, singular: str) -> str:
    if count == 1:
        return f"1 {singular}"
    return f"{count} {singular}s"


def _extract_work_item_id(ado_link: str) -> int | None:
    if not ado_link.strip():
        return None
    match = re.search(r"/(\d+)(?:[/?#]|$)", ado_link)
    if match is None:
        return None
    return int(match.group(1))


def _build_deck_render_context(
    *,
    issue_number: int,
    data_as_of: datetime,
    generated_at: datetime,
    title: str,
    source_label: str,
    area_path_count: int,
    manifest_id: str,
    dimension_risks: tuple[DimensionRisk, ...],
    top_items: tuple[Top3Item, ...],
    deltas: DeltaSet,
    scorecard_deltas: tuple[ScorecardDelta, ...],
    items: tuple[WorkItem, ...],
    eta_forecasts: dict[int, ETAForecast],
    raw_program: dict[str, object],
    program_id: str,
    programs_root: Path,
    milestones: tuple[Milestone, ...] = (),
    milestone_assessments: tuple[MilestoneAssessment, ...] = (),
    issue_projections: tuple[IssueProjection, ...] = (),
    key_decision_rows: tuple[DeckDecisionRow, ...] = (),
    key_assumption_rows: tuple[DeckAssumptionRow, ...] = (),
    open_ask_rows: tuple[DeckAskRow, ...],
    closed_ask_rows: tuple[DeckAskRow, ...],
) -> DeckRenderContext:
    item_lookup = {item.id: item for item in items}
    delta_lookup = _build_item_delta_lookup(deltas)
    return DeckRenderContext(
        issue_number=issue_number,
        issue_date_label=_format_deck_issue_date(data_as_of),
        health_rows=tuple(
            DeckHealthRow(
                dimension_name=dimension.name,
                risk=dimension.risk,
                summary=_truncate_deck_words(dimension.summary, 15),
            )
            for dimension in dimension_risks
        ),
        top_risk_rows=tuple(
            DeckTopRiskRow(
                text=item.text.strip(),
                risk=(item_lookup[work_item_id].risk_level if work_item_id in item_lookup else _risk_from_top_item_type(item.item_type)),
                delta_text=(_format_deck_item_delta(delta_lookup[work_item_id]) if work_item_id in delta_lookup else None),
                work_item_id=work_item_id,
            )
            for item in top_items
            for work_item_id in (_extract_work_item_id(item.ado_link),)
        ),
        change_rows=_build_deck_change_rows(deltas, scorecard_deltas),
        data_rows=(
            DeckDataRow(label="Title", value=title),
            DeckDataRow(label="Source", value=f"{source_label}, {area_path_count} area paths, {len(items)} items"),
            DeckDataRow(label="Generated", value=_format_deck_generated_at(generated_at)),
            DeckDataRow(label="Manifest", value=(manifest_id[:8] or "unknown")),
        ),
        telemetry_summary=build_program_telemetry_summary(
            program_id,
            programs_root=programs_root,
            as_of=data_as_of,
        ),
        telemetry_confidence=_build_deck_telemetry_confidence(
            program_id,
            programs_root=programs_root,
            as_of=data_as_of,
        ),
        charter_lines=_build_deck_charter_lines(raw_program),
        open_risk_rows=_build_deck_risk_rows(
            program_id=program_id,
            as_of=data_as_of,
            programs_root=programs_root,
        ),
        dependency_proposal_rows=_build_deck_dependency_proposal_rows(
            program_id=program_id,
            programs_root=programs_root,
        ),
        key_decision_rows=key_decision_rows,
        key_assumption_rows=key_assumption_rows,
        open_issue_rows=_build_deck_issue_rows(issue_projections, eta_forecasts=eta_forecasts),
        open_ask_rows=open_ask_rows,
        closed_ask_rows=closed_ask_rows,
        milestone_rows=_build_deck_milestone_rows(
            milestones,
            milestone_assessments,
            items=items,
            program_id=program_id,
            programs_root=programs_root,
            as_of=data_as_of,
        ),
    )


def _build_deck_telemetry_confidence(
    program_id: str,
    *,
    programs_root: Path,
    as_of: datetime,
) -> str | None:
    signal_store = build_signal_store_for_program_id(program_id, programs_root=programs_root)
    signals = signal_store.read(program_id, end=as_of)
    review_states = signal_store.read_reviews(program_id)
    telemetry_signals = [
        signal
        for signal in signals
        if signal_is_approved_for_evidence(signal, review_states)
        and signal.source in {"ado/analytics", "ado/wiql", "ado/sprint", "ado/pipeline", "ado/pr"}
    ]
    if not telemetry_signals:
        return None
    strongest = max(
        telemetry_signals,
        key=lambda signal: {
            "high": 3,
            "medium": 2,
            "low": 1,
            "none": 0,
        }.get(signal.confidence.value.lower(), 0),
    )
    return strongest.confidence.value.lower()


def _build_deck_charter_lines(raw_program: dict[str, object]) -> tuple[str, ...]:
    charter = raw_program.get("charter")
    if not isinstance(charter, dict):
        return ()

    lines: list[str] = []
    scope_statement = charter.get("scope_statement")
    if isinstance(scope_statement, str):
        normalized_scope = " ".join(scope_statement.strip().split())
        if normalized_scope:
            lines.append(f"Scope: {normalized_scope}")

    for key, label in (("success_criteria", "Success criterion"), ("constraints", "Constraint")):
        for value in normalize_charter_values(charter.get(key)):
            lines.append(f"{label}: {value}")

    return tuple(lines)


def _build_deck_issue_rows(
    issue_projections: tuple[IssueProjection, ...],
    *,
    eta_forecasts: dict[int, ETAForecast] | None = None,
) -> tuple[DeckIssueRow, ...]:
    eta_forecasts = eta_forecasts or {}
    return tuple(
        DeckIssueRow(
            title=entry.summary,
            detail=_format_deck_issue_detail(entry, eta_forecasts=eta_forecasts),
            href=entry.ado_url,
        )
        for entry in issue_projections
    )


def _build_deck_risk_rows(
    *,
    program_id: str,
    as_of: datetime,
    programs_root: Path,
) -> tuple[DeckRiskRow, ...]:
    risks = tuple(
        a.record
        for a in ProgramReality.load(program_id, programs_root=programs_root).risks()
        if a.record.status in {RiskStatus.OPEN, RiskStatus.ESCALATED}
    )
    ordered_risks = sorted(
        risks,
        key=lambda risk: (
            0 if risk.status == RiskStatus.ESCALATED else 1,
            -compute_risk_score(risk),
            risk.title.lower(),
        ),
    )
    return tuple(
        DeckRiskRow(
            title=risk.title,
            detail=_format_deck_risk_detail(risk, as_of=as_of),
        )
        for risk in ordered_risks
    )


def _build_deck_dependency_proposal_rows(
    *,
    program_id: str,
    programs_root: Path,
) -> tuple[DeckDependencyProposalRow, ...]:
    proposals = tuple(
        proposal
        for proposal in load_dependency_proposals(program_id, programs_root=programs_root)
        if proposal.status == DependencyProposalStatus.PROPOSED
    )
    return tuple(
        DeckDependencyProposalRow(
            title=(
                f"{proposal.id}: {proposal.from_workstream_id}:{proposal.from_item_id} -> "
                f"{proposal.to_workstream_id}:{proposal.to_item_id}"
            ),
            detail=(
                f"{proposal.detection_method} | {proposal.occurrence_count} signal(s) | {proposal.confidence.value.lower()} confidence | "
                f"accept via vertex dependencies accept --program {program_id} --id {proposal.id}"
            ),
        )
        for proposal in proposals[:5]
    )


def _format_deck_risk_detail(
    risk: RiskEntry,
    *,
    as_of: datetime,
) -> str:
    detail_parts = [risk.status.value.upper(), f"score {compute_risk_score(risk)}"]
    detail_parts.append("stale" if assess_risk_staleness(risk, as_of.date()) else "current")
    detail_parts.append(f"owner {risk.owner_alias}")
    if risk.mitigation_due_date is not None:
        detail_parts.append(f"mitigation due {risk.mitigation_due_date.isoformat()}")
    linked_refs: list[str] = []
    if risk.linked_workstream_ids:
        linked_refs.append(f"workstreams {', '.join(risk.linked_workstream_ids)}")
    if risk.linked_milestone_ids:
        linked_refs.append(f"milestones {', '.join(risk.linked_milestone_ids)}")
    if risk.linked_claim_ids:
        linked_refs.append(f"claims {', '.join(risk.linked_claim_ids)}")
    if linked_refs:
        detail_parts.append("linked " + " | ".join(linked_refs))
    if risk.mitigation_plan:
        detail_parts.append(risk.mitigation_plan)
    return " | ".join(detail_parts)


def _build_deck_decision_rows(
    *,
    program_id: str,
    as_of: datetime,
    programs_root: Path,
) -> tuple[DeckDecisionRow, ...]:
    decisions = tuple(
        a.record for a in ProgramReality.load(program_id, programs_root=programs_root).decisions()
    )
    return tuple(
        DeckDecisionRow(
            title=entry.title,
            detail=_format_deck_decision_detail(entry, as_of=as_of),
        )
        for entry in _sort_deck_decisions(decisions, as_of=as_of)
    )


def _build_deck_assumption_rows(
    *,
    program_id: str,
    as_of: datetime,
    programs_root: Path,
) -> tuple[DeckAssumptionRow, ...]:
    assumptions = tuple(
        a.record for a in ProgramReality.load(program_id, programs_root=programs_root).assumptions()
    )
    overdue_ids = {entry.id for entry in check_validation_due(assumptions, as_of.date())}
    return tuple(
        DeckAssumptionRow(
            title=entry.text,
            detail=_format_deck_assumption_detail(entry, overdue=entry.id in overdue_ids),
        )
        for entry in _sort_deck_assumptions(assumptions, overdue_ids=overdue_ids)
    )


def _sort_deck_decisions(entries: tuple[DecisionEntry, ...], *, as_of: datetime) -> tuple[DecisionEntry, ...]:
    return tuple(
        sorted(
            entries,
            key=lambda entry: (
                0 if entry.status is DecisionStatus.PROPOSED else 1,
                0 if assess_proposed_decision_staleness(entry, as_of.date()) else 1,
                -(entry.decision_date.toordinal() if entry.decision_date is not None else 0),
                entry.title.lower(),
            ),
        )
    )


def _sort_deck_assumptions(
    entries: tuple[Assumption, ...],
    *,
    overdue_ids: set[str],
) -> tuple[Assumption, ...]:
    return tuple(
        sorted(
            entries,
            key=lambda entry: (
                0 if entry.id in overdue_ids else 1,
                0 if entry.status is AssumptionStatus.UNVALIDATED else 1,
                entry.validation_due or date.max,
                entry.identified_date,
                entry.text.lower(),
            ),
        )
    )


def _format_deck_decision_detail(entry: DecisionEntry, *, as_of: datetime) -> str:
    details = [entry.decision, entry.status.value.upper()]
    if entry.status is DecisionStatus.PROPOSED:
        details.append("stale" if assess_proposed_decision_staleness(entry, as_of.date()) else "current")
    details.append(f"owner {entry.decided_by}")
    details.append(f"date {entry.decision_date.isoformat() if entry.decision_date is not None else 'TBD'}")
    if entry.workstream_id is not None:
        details.append(f"workstream {entry.workstream_id}")
    return " | ".join(details)


def _format_deck_assumption_detail(entry: Assumption, *, overdue: bool) -> str:
    details = [entry.status.value.upper(), "overdue" if overdue else "current"]
    if entry.validation_due is not None:
        details.append(f"due {entry.validation_due.isoformat()}")
    if entry.owner_alias is not None:
        details.append(f"owner {entry.owner_alias}")
    if entry.linked_milestone_id is not None:
        details.append(f"milestone {entry.linked_milestone_id}")
    if entry.linked_risk_id is not None:
        details.append(f"risk {entry.linked_risk_id}")
    return " | ".join(details)


def _format_deck_issue_detail(
    entry: IssueProjection,
    *,
    eta_forecasts: dict[int, ETAForecast],
) -> str:
    details = [issue_projection_source_label(entry), entry.severity.upper(), issue_projection_confidence_label(entry)]
    forecast = eta_forecasts.get(entry.work_item_id) if entry.work_item_id is not None else None
    if forecast is not None and forecast.display_annotation is not None:
        details.append(forecast.display_annotation)
    if entry.owner_alias is not None:
        details.append(f"owner {entry.owner_alias}")
    if entry.workstream_id is not None:
        details.append(f"workstream {entry.workstream_id}")
    if entry.linked_entity_ids:
        details.append(f"linked {', '.join(entry.linked_entity_ids)}")
    return " | ".join(details)


def _build_deck_milestone_rows(
    milestones: tuple[Milestone, ...],
    milestone_assessments: tuple[MilestoneAssessment, ...],
    *,
    items: tuple[WorkItem, ...],
    program_id: str,
    programs_root: Path,
    as_of: datetime,
    milestone_lineage: dict[str, dict[str, str | None]] | None = None,
) -> tuple[DeckMilestoneRow, ...]:
    if not milestones or not milestone_assessments:
        return ()
    assessment_map = {assessment.milestone_id: assessment for assessment in milestone_assessments}
    trajectory_store = build_trajectory_store_for_program_id(
        program_id,
        programs_root=programs_root,
    )
    trajectories = {
        item_id: trajectory_store.read(program_id, item_id)
        for milestone in milestones
        for item_id in milestone.linked_work_item_ids
    }
    target_date_history = load_milestone_target_date_history_map(
        program_id,
        milestones,
        programs_root=programs_root,
    )
    completion_date_history = load_milestone_completion_date_history_map(
        program_id,
        milestones,
        current_completion_dates={assessment.milestone_id: assessment.completion_date for assessment in milestone_assessments},
        programs_root=programs_root,
    )
    rows: list[DeckMilestoneRow] = []
    for milestone in milestones:
        assessment = assessment_map.get(milestone.id)
        if assessment is None:
            continue
        lineage = (milestone_lineage or {}).get(milestone.id, {})
        rows.append(
            DeckMilestoneRow(
                name=milestone.name,
                status=_format_milestone_status_label(assessment.computed_health, critical_path=assessment.critical_path),
                target_date_label=milestone.target_date.strftime("%b %d"),
                detail=_format_deck_milestone_detail(
                    assessment,
                    schedule_summary=describe_milestone_schedule_variance(milestone, items, trajectories, as_of),
                    target_history_summary=summarize_milestone_target_date_history(
                        target_date_history.get(milestone.id, ()),
                        prefix="target history",
                    ),
                    completion_history_summary=summarize_milestone_completion_date_history(
                        completion_date_history.get(milestone.id, ()),
                        prefix="completion history",
                    ),
                ),
                source_document_key=lineage.get("source_document_key"),
                approval_event_id=lineage.get("approval_event_id"),
            )
        )
    return tuple(rows)


def _build_report_milestone_rows(
    milestones: tuple[Milestone, ...],
    milestone_assessments: tuple[MilestoneAssessment, ...],
    *,
    items: tuple[WorkItem, ...],
    program_id: str,
    programs_root: Path,
    as_of: datetime,
    milestone_lineage: dict[str, dict[str, str | None]] | None = None,
) -> tuple[MilestoneSummaryRow, ...]:
    return tuple(
        MilestoneSummaryRow(
            name=row.name,
            status=row.status,
            target_date_label=row.target_date_label,
            detail=row.detail,
            source_document_key=row.source_document_key,
            approval_event_id=row.approval_event_id,
        )
        for row in _build_deck_milestone_rows(
            milestones,
            milestone_assessments,
            items=items,
            program_id=program_id,
            programs_root=programs_root,
            as_of=as_of,
            milestone_lineage=milestone_lineage,
        )
    )


def _format_deck_milestone_detail(
    assessment: MilestoneAssessment,
    *,
    schedule_summary: str | None = None,
    target_history_summary: str | None = None,
    completion_history_summary: str | None = None,
) -> str:
    blocked_count = len(assessment.blocked_criteria)
    blocked_label = "no blocked signals"
    if blocked_count == 1:
        blocked_label = "1 blocked signal"
    elif blocked_count > 1:
        blocked_label = f"{blocked_count} blocked signals"
    parts = [
        f"{blocked_label}; {round(assessment.slip_probability * 100)}% slip probability; {assessment.confidence.value} confidence"
    ]
    if schedule_summary:
        parts.append(schedule_summary.lower())
    if target_history_summary:
        parts.append(target_history_summary)
    if completion_history_summary:
        parts.append(completion_history_summary)
    return "; ".join(parts)


def _format_milestone_status_label(status: MilestoneStatus, *, critical_path: bool) -> str:
    label = status.value.replace("_", " ")
    if critical_path:
        return f"{label} (critical path)"
    return label


def _build_deck_ask_rows(
    *,
    program_id: str,
    issue_number: int,
    as_of: datetime,
    last_confirmed_at: datetime | None,
    programs_root: Path,
) -> tuple[tuple[DeckAskRow, ...], tuple[DeckAskRow, ...]]:
    latest_statuses = load_latest_claim_statuses(program_id, programs_root=programs_root)
    open_rows = tuple(
        DeckAskRow(
            title=f"Issue #{entry.issue_number:03d}",
            detail=_format_deck_open_ask_row(entry.text, owner_alias=entry.owner_alias),
        )
        for entry in load_open_decision_asks(program_id, programs_root=programs_root)
    )
    closed_rows: list[DeckAskRow] = []
    for entry in load_decision_asks(program_id, programs_root=programs_root):
        status_update = latest_statuses.get(entry.id)
        if status_update is None or status_update.new_status not in {"resolved", "deferred"}:
            continue
        if entry.issue_number >= issue_number:
            continue
        if status_update.updated_at > as_of:
            continue
        if last_confirmed_at is not None and status_update.updated_at <= last_confirmed_at:
            continue
        closed_rows.append(
            DeckAskRow(
                title=f"Issue #{entry.issue_number:03d} · {status_update.new_status}",
                detail=_format_deck_closed_ask_row(entry.text, status_update.note),
            )
        )
    return open_rows, tuple(closed_rows)


def _format_deck_open_ask_row(text: str, *, owner_alias: str | None) -> str:
    if owner_alias:
        return f"{text} (owner {owner_alias})"
    return text


def _format_deck_closed_ask_row(text: str, note: str | None) -> str:
    if note is not None and note.strip():
        return f"{text} ({note.strip()})"
    return text


def _build_deck_change_rows(
    deltas: DeltaSet,
    scorecard_deltas: tuple[ScorecardDelta, ...],
) -> tuple[DeckChangeRow, ...]:
    if deltas.previous_issue_number is None:
        return (DeckChangeRow(text="No prior confirmed snapshot is available; this issue establishes the baseline."),)

    rows = [
        DeckChangeRow(text=f"{scorecard_delta.dimension}: {_format_deck_risk_change(scorecard_delta.old_risk, scorecard_delta.new_risk)}")
        for scorecard_delta in scorecard_deltas
    ]
    counts_summary = _format_deck_delta_counts_summary(deltas)
    if counts_summary is not None:
        rows.append(DeckChangeRow(text=counts_summary))
    if not rows:
        rows.append(DeckChangeRow(text="No material changes were detected against the prior confirmed snapshot."))
    return tuple(rows)


def _build_item_delta_lookup(deltas: DeltaSet) -> dict[int, ItemDelta]:
    ordered_deltas = [
        *sorted((delta for delta in deltas.risk_changes if delta.kind == DeltaKind.RISK_UP), key=lambda delta: delta.work_item_id),
        *sorted(deltas.new_items, key=lambda delta: delta.work_item_id),
        *sorted(deltas.eta_changes, key=lambda delta: delta.work_item_id),
        *sorted(tuple(getattr(deltas, "owner_changes", ())), key=lambda delta: delta.work_item_id),
        *sorted((delta for delta in deltas.risk_changes if delta.kind == DeltaKind.RISK_DOWN), key=lambda delta: delta.work_item_id),
        *sorted(deltas.closed_items, key=lambda delta: delta.work_item_id),
    ]
    lookup: dict[int, ItemDelta] = {}
    for delta in ordered_deltas:
        lookup.setdefault(delta.work_item_id, delta)
    return lookup


def _format_deck_delta_counts_summary(deltas: DeltaSet) -> str | None:
    counts: list[str] = []
    if deltas.new_items:
        counts.append(_count_label(len(deltas.new_items), "new item"))
    if deltas.closed_items:
        counts.append(_count_label(len(deltas.closed_items), "closed item"))
    if deltas.eta_changes:
        counts.append(_count_label(len(deltas.eta_changes), "ETA shift"))
    owner_changes = tuple(getattr(deltas, "owner_changes", ()))
    if owner_changes:
        counts.append(_count_label(len(owner_changes), "owner change"))
    if not counts:
        return None
    return ", ".join(counts)
