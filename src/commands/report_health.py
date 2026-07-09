from __future__ import annotations

from datetime import datetime
from typing import Literal
from pathlib import Path

from src.core.exceptions import ConfigError
from src.core.forecast_engine import ForecastAssessment
from src.core.milestone_engine import describe_milestone_schedule_variance, load_milestone_completion_date_history_map, load_milestone_target_date_history_map, summarize_milestone_completion_date_history, summarize_milestone_target_date_history
from src.core.models import Confidence, DimensionRisk, EditionType, RiskLevel, Snapshot, WorkItem
from src.core.models_v2 import Milestone, MilestoneAssessment, MilestoneStatus, RiskEntry, RiskStatus
from src.core.overrides_store import OverridesDocument
from src.core.program_reality import FactAssessment, ProgramReality
from src.core.risk_register_engine import assess_risk_staleness, compute_risk_score
from src.core.signal_review import signal_is_approved_for_evidence
from src.core.store_factory import build_signal_store_for_program_id, build_trajectory_store_for_program_id
from src.core.telemetry_summary import build_program_telemetry_summary
from src.core.view_models import HealthSummary, Top3Item


_RISK_LOAD_WEIGHTS = {
    RiskLevel.HIGH: 3,
    RiskLevel.MEDIUM: 2,
    RiskLevel.LOW: 1,
    RiskLevel.DONE: 0,
    RiskLevel.UNKNOWN: 0,
}


def _compute_risk_load(dimension_risks: tuple[DimensionRisk, ...]) -> float:
    if not dimension_risks:
        return 0.0
    total = sum(_RISK_LOAD_WEIGHTS.get(dimension.risk, 0) for dimension in dimension_risks)
    return round(total / len(dimension_risks), 1)


def _compute_prior_risk_load(previous_snapshot: Snapshot | None) -> float | None:
    if previous_snapshot is None or not previous_snapshot.scorecards:
        return None
    total = sum(_RISK_LOAD_WEIGHTS.get(dimension.risk, 0) for dimension in previous_snapshot.scorecards)
    return round(total / len(previous_snapshot.scorecards), 1)


def _edition_label(edition_type: EditionType) -> str:
    labels = {
        EditionType.DETAILED: "Detailed Edition",
        EditionType.FOCUSED: "Focused Edition",
        EditionType.CONDENSED: "Condensed Edition",
        EditionType.DECK: "Deck Edition",
        EditionType.NARRATIVE: "Narrative Edition",
        EditionType.LOOKBACK: "Lookback Edition",
    }
    return labels.get(edition_type, "Detailed Edition")


def _truncate_words(text: str, word_limit: int) -> str:
    words = text.strip().split()
    if len(words) <= word_limit:
        return text.strip()
    return " ".join(words[:word_limit]).rstrip(".,;:") + "."


def _resolve_health_bluf(
    overrides_document: OverridesDocument,
    dimension_risks: tuple[DimensionRisk, ...],
) -> str | None:
    if overrides_document.health_bluf is not None and overrides_document.health_bluf.strip():
        return _truncate_words(overrides_document.health_bluf.strip(), 25)
    high_count = sum(1 for dimension in dimension_risks if dimension.risk == RiskLevel.HIGH)
    if high_count:
        return f"{high_count} of {len(dimension_risks)} dimensions at High risk."
    return None


def _normalize_leadership_ask(text: str) -> str:
    return " ".join(text.strip().split())


def _resolve_leadership_ask(
    overrides_document: OverridesDocument,
    top_items: tuple[Top3Item, ...],
    *,
    severe_ack_required: bool,
    is_dry_run: bool,
    all_green: bool,
) -> str:
    if overrides_document.leadership_ask is not None and overrides_document.leadership_ask.strip():
        return f"Leadership ask: {_normalize_leadership_ask(overrides_document.leadership_ask)}"
    if top_items:
        first_item = top_items[0]
        ask_text = _normalize_leadership_ask(first_item.text)
        return f"Leadership ask: {ask_text}"
    if all_green:
        return "Leadership ask: None — maintain current execution cadence."
    if severe_ack_required and is_dry_run:
        return "Leadership ask: Author confirmation required before publish."
    return "Leadership ask: None this week."


def _compute_trajectory(
    *,
    risk_load: float,
    prior_risk_load: float | None,
    new_high_count: int,
    high_count: int,
    medium_count: int,
) -> tuple[Literal["improving", "stable", "degrading"], str | None]:
    health_reason: str | None = None
    trajectory: Literal["improving", "stable", "degrading"] = "stable"
    if prior_risk_load is not None:
        if risk_load < prior_risk_load - 0.15:
            trajectory = "improving"
        elif risk_load > prior_risk_load + 0.15:
            trajectory = "degrading"
    if new_high_count:
        trajectory = "degrading"
        health_reason = "New High this issue (override: trajectory Degrading)"
    elif high_count >= 3:
        health_reason = "3 High dimensions (override: Critical threshold)"
    elif high_count == 0 and medium_count == 0:
        trajectory = "stable"
    return trajectory, health_reason


def _build_health_summary(
    dimension_risks: tuple[DimensionRisk, ...],
    previous_snapshot: Snapshot | None,
    *,
    overrides_document: OverridesDocument | None = None,
    top_items: tuple[Top3Item, ...] = (),
    forecast: ForecastAssessment | None = None,
    items: tuple[WorkItem, ...] = (),
    milestones: tuple[Milestone, ...] = (),
    milestone_assessments: tuple[MilestoneAssessment, ...] = (),
    risks: tuple[RiskEntry, ...] = (),
    risk_assessments: tuple[FactAssessment, ...] = (),
    stale_risk_ids: tuple[str, ...] = (),
    program_id: str | None = None,
    programs_root: Path | None = None,
    as_of: datetime | None = None,
    reality: ProgramReality | None = None,
    severe_ack_required: bool = False,
    is_dry_run: bool = True,
    read_time_minutes: int | None = None,
    edition_type: EditionType = EditionType.DETAILED,
    new_high_count: int = 0,
    healthy_streak: int = 0,
    status_note: str | None = None,
) -> HealthSummary:
    counts = {
        RiskLevel.HIGH: sum(1 for dimension in dimension_risks if dimension.risk == RiskLevel.HIGH),
        RiskLevel.MEDIUM: sum(1 for dimension in dimension_risks if dimension.risk == RiskLevel.MEDIUM),
        RiskLevel.LOW: sum(1 for dimension in dimension_risks if dimension.risk == RiskLevel.LOW),
        RiskLevel.DONE: sum(1 for dimension in dimension_risks if dimension.risk == RiskLevel.DONE),
    }
    total_count = sum(counts.values())
    prior_counts: dict[str, int] | None = None
    delta_direction: Literal["improved", "degraded", "unchanged"] = "unchanged"
    risk_load = _compute_risk_load(dimension_risks)
    prior_risk_load = _compute_prior_risk_load(previous_snapshot)
    if previous_snapshot is not None:
        prior_counts = {
            "high": sum(1 for dimension in previous_snapshot.scorecards if dimension.risk == RiskLevel.HIGH),
            "medium": sum(1 for dimension in previous_snapshot.scorecards if dimension.risk == RiskLevel.MEDIUM),
            "low": sum(1 for dimension in previous_snapshot.scorecards if dimension.risk == RiskLevel.LOW),
            "done": sum(1 for dimension in previous_snapshot.scorecards if dimension.risk == RiskLevel.DONE),
        }
        current_score = counts[RiskLevel.HIGH] * 3 + counts[RiskLevel.MEDIUM] * 2 + counts[RiskLevel.LOW]
        prior_score = prior_counts["high"] * 3 + prior_counts["medium"] * 2 + prior_counts["low"]
        if current_score < prior_score:
            delta_direction = "improved"
        elif current_score > prior_score:
            delta_direction = "degraded"
    overall_risk = RiskLevel.UNKNOWN
    if counts[RiskLevel.HIGH]:
        overall_risk = RiskLevel.HIGH
    elif counts[RiskLevel.MEDIUM]:
        overall_risk = RiskLevel.MEDIUM
    elif counts[RiskLevel.LOW]:
        overall_risk = RiskLevel.LOW
    elif counts[RiskLevel.DONE] and counts[RiskLevel.DONE] == total_count:
        overall_risk = RiskLevel.DONE

    trajectory, health_reason = _compute_trajectory(
        risk_load=risk_load,
        prior_risk_load=prior_risk_load,
        new_high_count=new_high_count,
        high_count=counts[RiskLevel.HIGH],
        medium_count=counts[RiskLevel.MEDIUM],
    )
    all_green = counts[RiskLevel.LOW] + counts[RiskLevel.DONE] == total_count and total_count > 0
    resolved_overrides = overrides_document or OverridesDocument(issue_number=None, top_3_now=(), scorecards=())
    bluf = _resolve_health_bluf(resolved_overrides, dimension_risks)
    leadership_ask = _resolve_leadership_ask(
        resolved_overrides,
        top_items,
        severe_ack_required=severe_ack_required,
        is_dry_run=is_dry_run,
        all_green=all_green,
    )
    risk_bar_width = int(round((max(0.0, min(risk_load / 3.0, 1.0))) * 80))
    resolved_risks = risks
    resolved_stale_risk_ids = stale_risk_ids
    resolved_risk_assessments: tuple[FactAssessment, ...] = risk_assessments
    if not resolved_risks and program_id is not None and programs_root is not None:
        try:
            resolved_reality = reality or ProgramReality.load(program_id, programs_root=programs_root)
            resolved_risk_assessments = tuple(resolved_reality.risks())
            resolved_risks = tuple(assessment.record for assessment in resolved_risk_assessments)
        except ConfigError:
            resolved_risks = ()
        if not resolved_stale_risk_ids and as_of is not None:
            resolved_stale_risk_ids = tuple(
                risk.id
                for risk in resolved_risks
                if assess_risk_staleness(risk, as_of.date())
            )
    if resolved_risks and not resolved_risk_assessments:
        resolved_risk_assessments = ()
    highlighted_risk_assessment = _select_active_risk_assessment(
        resolved_risk_assessments,
        stale_risk_ids=resolved_stale_risk_ids,
    )

    return HealthSummary(
        overall_risk=overall_risk,
        high_count=counts[RiskLevel.HIGH],
        medium_count=counts[RiskLevel.MEDIUM],
        low_count=counts[RiskLevel.LOW],
        done_count=counts[RiskLevel.DONE],
        total_count=total_count,
        delta_direction=delta_direction,
        prior_counts=prior_counts,
        trajectory=trajectory,
        bluf=bluf,
        leadership_ask=leadership_ask,
        risk_load=risk_load,
        prior_risk_load=prior_risk_load,
        risk_load_bar_width=risk_bar_width,
        healthy_streak=healthy_streak if all_green else 0,
        read_time_minutes=read_time_minutes or 1,
        edition_label=_edition_label(edition_type),
        health_reason=health_reason,
        forecast_summary=(forecast.published_summary if forecast is not None else None),
        forecast_confidence=(forecast.confidence.value if forecast is not None else None),
        status_note=status_note,
        milestone_summary=_build_milestone_health_summary(
            milestones,
            milestone_assessments,
            items=items,
            program_id=program_id,
            programs_root=programs_root,
            as_of=as_of,
        ),
        risk_register_summary=_build_risk_register_summary(
            resolved_risks,
            stale_risk_ids=resolved_stale_risk_ids,
        ),
        telemetry_summary=(
            build_program_telemetry_summary(
                program_id,
                programs_root=programs_root,
                as_of=as_of,
            )
            if program_id is not None and programs_root is not None and as_of is not None
            else None
        ),
        telemetry_confidence=(
            _build_health_telemetry_confidence(
                program_id,
                programs_root=programs_root,
                as_of=as_of,
            )
            if program_id is not None and programs_root is not None and as_of is not None
            else None
        ),
        risk_register_truth_level=(
            highlighted_risk_assessment.truth_level.value if highlighted_risk_assessment is not None else None
        ),
        risk_register_disputed=(
            highlighted_risk_assessment.disputed if highlighted_risk_assessment is not None else False
        ),
        risk_register_stale_evidence=(
            highlighted_risk_assessment.stale if highlighted_risk_assessment is not None else False
        ),
        risk_register_includes_unconfirmed_sources=any(
            assessment.truth_level.value == "raw_observed"
            for assessment in resolved_risk_assessments
        ),
    )


def _select_active_risk_assessment(
    assessments: tuple[FactAssessment, ...],
    *,
    stale_risk_ids: tuple[str, ...] = (),
) -> FactAssessment | None:
    active_assessments = tuple(
        assessment
        for assessment in assessments
        if assessment.record.status in {RiskStatus.OPEN, RiskStatus.ESCALATED}
    )
    if not active_assessments:
        return None
    stale_risk_id_set = set(stale_risk_ids)
    return sorted(
        active_assessments,
        key=lambda assessment: (
            0 if assessment.record.status == RiskStatus.ESCALATED else 1,
            -compute_risk_score(assessment.record),
            0 if assessment.record.id in stale_risk_id_set else 1,
            assessment.record.title.lower(),
        ),
    )[0]


def _build_health_telemetry_confidence(
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
    confidence_order = {
        Confidence.HIGH: 3,
        Confidence.MEDIUM: 2,
        Confidence.LOW: 1,
        Confidence.NONE: 0,
    }
    return max(telemetry_signals, key=lambda signal: confidence_order[signal.confidence]).confidence.value.lower()


def _build_milestone_health_summary(
    milestones: tuple[Milestone, ...],
    milestone_assessments: tuple[MilestoneAssessment, ...],
    *,
    items: tuple[WorkItem, ...] = (),
    program_id: str | None = None,
    programs_root: Path | None = None,
    as_of: datetime | None = None,
) -> str | None:
    if not milestones or not milestone_assessments:
        return None
    assessment_map = {assessment.milestone_id: assessment for assessment in milestone_assessments}
    ordered_assessments = tuple(
        assessment_map[milestone.id]
        for milestone in milestones
        if milestone.id in assessment_map
    )
    if not ordered_assessments:
        return None

    count_parts: list[str] = []
    for status in (MilestoneStatus.MISSED, MilestoneStatus.AT_RISK, MilestoneStatus.ON_TRACK, MilestoneStatus.COMPLETED, MilestoneStatus.DEFERRED):
        count = sum(1 for assessment in ordered_assessments if assessment.computed_health == status)
        if count == 0:
            continue
        count_parts.append(f"{count} {status.value.replace('_', ' ')}")

    critical_path_names = [
        milestone.name
        for milestone in milestones
        if (assessment := assessment_map.get(milestone.id)) is not None and assessment.critical_path
    ]
    critical_summary = ""
    if critical_path_names:
        critical_summary = f" Critical path: {', '.join(critical_path_names[:2])}."

    highlight_summary = ""
    if items and program_id is not None and as_of is not None:
        resolved_programs_root = programs_root if programs_root is not None else Path("programs")
        trajectory_store = build_trajectory_store_for_program_id(
            program_id,
            programs_root=resolved_programs_root,
        )
        trajectories = {
            item_id: trajectory_store.read(program_id, item_id)
            for milestone in milestones
            for item_id in milestone.linked_work_item_ids
        }
        target_date_history = load_milestone_target_date_history_map(
            program_id,
            milestones,
            programs_root=resolved_programs_root,
        )
        completion_date_history = load_milestone_completion_date_history_map(
            program_id,
            milestones,
            current_completion_dates={assessment.milestone_id: assessment.completion_date for assessment in ordered_assessments},
            programs_root=resolved_programs_root,
        )
        highlights: list[str] = []
        for milestone in milestones:
            assessment = assessment_map.get(milestone.id)
            if assessment is None or assessment.computed_health not in {MilestoneStatus.MISSED, MilestoneStatus.AT_RISK, MilestoneStatus.COMPLETED}:
                continue
            detail_parts: list[str] = []
            schedule_summary = describe_milestone_schedule_variance(milestone, items, trajectories, as_of)
            if schedule_summary:
                detail_parts.append(schedule_summary)
            target_history_summary = summarize_milestone_target_date_history(
                target_date_history.get(milestone.id, ()),
                prefix="target history",
            )
            if target_history_summary:
                detail_parts.append(target_history_summary)
            completion_history_summary = summarize_milestone_completion_date_history(
                completion_date_history.get(milestone.id, ()),
                prefix="completion history",
            )
            if completion_history_summary:
                detail_parts.append(completion_history_summary)
            if detail_parts:
                highlights.append(f"{milestone.name}: {'; '.join(detail_parts)}")
            if len(highlights) == 2:
                break
        if highlights:
            highlight_summary = f" Highlights: {' | '.join(highlights)}."
    return f"Milestones: {', '.join(count_parts)}.{highlight_summary}{critical_summary}"


def _build_risk_register_summary(
    risks: tuple[RiskEntry, ...],
    *,
    stale_risk_ids: tuple[str, ...] = (),
) -> str | None:
    if not risks:
        return None

    active_risks = tuple(
        risk
        for risk in risks
        if risk.status in {RiskStatus.OPEN, RiskStatus.ESCALATED}
    )
    if not active_risks:
        return None

    stale_risk_id_set = set(stale_risk_ids)
    escalated_count = sum(1 for risk in active_risks if risk.status == RiskStatus.ESCALATED)
    stale_count = sum(1 for risk in active_risks if risk.id in stale_risk_id_set)
    highest_risk = sorted(
        active_risks,
        key=lambda risk: (
            0 if risk.status == RiskStatus.ESCALATED else 1,
            -compute_risk_score(risk),
            risk.title.lower(),
        ),
    )[0]

    summary = f"Risk register: {len(active_risks)} active entr{'y' if len(active_risks) == 1 else 'ies'}"
    if escalated_count:
        summary += f" ({escalated_count} escalated)"
    if stale_count:
        summary += f", {stale_count} stale review{'s' if stale_count != 1 else ''}."
    else:
        summary += ", reviews current."
    summary += (
        f" Highest active: {highest_risk.title} "
        f"(owner {highest_risk.owner_alias}, score {compute_risk_score(highest_risk)})."
    )
    return summary