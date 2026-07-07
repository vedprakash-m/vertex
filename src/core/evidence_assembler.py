from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone

from src.core.claim_tracker import assess_claim_entries
from src.core.delta_engine import build_deltas
from src.core.metric_models import MetricObservation
from src.core.models import Confidence, RiskLevel, Snapshot, WorkItem
from src.core.models_v2 import ClaimEntry, DecisionEntry, MetricEvidenceBrief, PersonDirectory, SectionEvidenceBrief, Signal, VitalityScore, WorkstreamEvidencePacket
from src.core.chronicle import ProgramEvent
from src.core.external_dependency import ExternalDependency
from src.core.signal_ranking import sort_signals_for_ai_context
from src.core.view_models import KpiTile

_ADAPTIVE_WINDOW_MIN_DAYS = 7
_ADAPTIVE_WINDOW_MAX_DAYS = 45

_RISK_SEVERITY: dict[RiskLevel, int] = {
    RiskLevel.BLOCKED: 5,
    RiskLevel.HIGH: 4,
    RiskLevel.MEDIUM: 3,
    RiskLevel.LOW: 2,
    RiskLevel.DONE: 1,
    RiskLevel.UNKNOWN: 0,
}
_SUPPRESSION_RISK_LEVELS = {RiskLevel.LOW, RiskLevel.DONE}


def assemble_section_evidence_brief(
    section_id: str,
    workstream_id: str | None,
    *,
    current_items: tuple[WorkItem, ...],
    previous_snapshot: Snapshot | None,
    journal_signals: tuple[Signal, ...],
    vitality_scores: tuple[VitalityScore, ...],
    kpi_tiles: tuple[KpiTile, ...],
    claims: tuple[ClaimEntry, ...],
    issue_number: int,
    as_of: datetime,
    people_directory: tuple[PersonDirectory, ...] = (),
    source_confidence_order: tuple[str, ...] = (),
    metric_observations: tuple[MetricObservation, ...] = (),
    cadence_days: float | None = None,
) -> SectionEvidenceBrief:
    resolved_as_of = _ensure_utc(as_of)
    deltas = build_deltas(
        current_items,
        previous_snapshot,
        issue_number,
        previous_snapshot.issue_number if previous_snapshot is not None else None,
    )
    # FR-SG-34: compute adaptive window from signal history for this workstream
    window_days = compute_adaptive_signal_window(
        journal_signals,
        workstream_id=workstream_id,
        cadence_days=cadence_days,
    )
    scoped_signals = _scoped_recent_signals(
        journal_signals,
        workstream_id=workstream_id,
        as_of=resolved_as_of,
        window_days=window_days,
    )
    ranked_signals = sort_signals_for_ai_context(
        scoped_signals,
        people_directory=people_directory,
        as_of=resolved_as_of,
        source_confidence_order=source_confidence_order,
    )
    scoped_vitality = _scoped_vitality_scores(vitality_scores, workstream_id=workstream_id)
    stale_claims = _stale_claim_ids(claims, items=current_items, workstream_id=workstream_id, as_of=resolved_as_of)
    include_kpis = workstream_id is not None

    # FR-SG-23: activity score = count of distinct change events in this slice
    activity_score = float(
        len(deltas.new_items)
        + len(deltas.closed_items)
        + len(deltas.risk_changes)
        + len(deltas.eta_changes)
        + (1 if scoped_signals else 0)
    )
    section_max_risk = _compute_section_max_risk(current_items)
    suppression_suggested = activity_score == 0.0 and section_max_risk in _SUPPRESSION_RISK_LEVELS

    # FR-SG-15: convert MetricObservation records to compact MetricEvidenceBrief DTOs
    metric_briefs = tuple(
        _metric_observation_to_brief(obs, as_of=resolved_as_of)
        for obs in metric_observations
    )

    return SectionEvidenceBrief(
        section_id=section_id,
        ado_delta_summary=_format_delta_summary(deltas),
        new_items=tuple(delta.work_item_id for delta in deltas.new_items),
        closed_items=tuple(delta.work_item_id for delta in deltas.closed_items),
        risk_changed_items=tuple(delta.work_item_id for delta in deltas.risk_changes),
        eta_changed_items=tuple(delta.work_item_id for delta in deltas.eta_changes),
        top_signals=tuple(signal.id for signal in ranked_signals[: (5 if workstream_id is None else 3)]),
        kpi_summary=_format_kpi_summary(kpi_tiles) if include_kpis else None,
        stale_claims=stale_claims,
        vitality_summary=_format_vitality_summary(scoped_vitality),
        confidence=_confidence_label(
            has_ado=bool(current_items),
            has_signals=bool(ranked_signals),
            has_kpis=(not include_kpis) or bool(kpi_tiles),
        ),
        activity_score=activity_score,
        suppression_suggested=suppression_suggested,
        metric_observations=metric_briefs,
    )


def compute_adaptive_signal_window(
    signals: tuple[Signal, ...],
    *,
    workstream_id: str | None,
    cadence_days: float | None = None,
) -> int:
    """FR-SG-34: Compute per-workstream adaptive evidence window in days.

    window = clamp(7, cadence*1.5 or 3*median_signal_interval, 45)
    Falls back to the 7-day minimum if insufficient signal history.
    """
    ws_signals = sorted(
        (s for s in signals if workstream_id is None or s.workstream_id == workstream_id),
        key=lambda s: s.timestamp,
    )
    if cadence_days is not None:
        target = cadence_days * 1.5
    elif len(ws_signals) >= 2:
        intervals = [
            (ws_signals[i].timestamp - ws_signals[i - 1].timestamp).total_seconds() / 86400.0
            for i in range(1, len(ws_signals))
        ]
        median_interval = statistics.median(intervals)
        target = median_interval * 3.0
    else:
        target = _ADAPTIVE_WINDOW_MIN_DAYS
    return int(max(_ADAPTIVE_WINDOW_MIN_DAYS, min(_ADAPTIVE_WINDOW_MAX_DAYS, target)))


def _scoped_recent_signals(
    signals: tuple[Signal, ...],
    *,
    workstream_id: str | None,
    as_of: datetime,
    window_days: int | None = None,
) -> tuple[Signal, ...]:
    effective_window = window_days if window_days is not None else _ADAPTIVE_WINDOW_MIN_DAYS
    window_start = as_of - timedelta(days=effective_window)
    scoped = []
    for signal in signals:
        timestamp = _ensure_utc(signal.timestamp)
        if timestamp < window_start or timestamp > as_of:
            continue
        if workstream_id is not None and signal.workstream_id != workstream_id:
            continue
        scoped.append(signal)
    return tuple(scoped)


def _scoped_vitality_scores(
    vitality_scores: tuple[VitalityScore, ...],
    *,
    workstream_id: str | None,
) -> tuple[VitalityScore, ...]:
    if workstream_id is None:
        return vitality_scores
    return tuple(score for score in vitality_scores if score.workstream_id == workstream_id)


def _stale_claim_ids(
    claims: tuple[ClaimEntry, ...],
    *,
    items: tuple[WorkItem, ...],
    workstream_id: str | None,
    as_of: datetime,
) -> tuple[str, ...]:
    scoped_claims = tuple(
        claim
        for claim in claims
        if workstream_id is None or claim.workstream_id == workstream_id
    )
    assessments = assess_claim_entries(scoped_claims, items=items, as_of=as_of)
    return tuple(
        assessment.claim.id
        for assessment in assessments
        if assessment.effective_status in {"stale", "contradicted"}
    )


def _format_delta_summary(deltas) -> str:
    parts: list[str] = []
    if deltas.new_items:
        parts.append(f"{len(deltas.new_items)} new")
    if deltas.closed_items:
        parts.append(f"{len(deltas.closed_items)} closed")
    if deltas.risk_changes:
        parts.append(f"{len(deltas.risk_changes)} risk changed")
    if deltas.eta_changes:
        parts.append(f"{len(deltas.eta_changes)} ETA changed")
    if not parts:
        return "No ADO deltas in scope."
    return "; ".join(parts) + "."


def _format_kpi_summary(kpi_tiles: tuple[KpiTile, ...]) -> str | None:
    if not kpi_tiles:
        return None
    return "; ".join(_format_kpi_tile(tile) for tile in kpi_tiles)


def _format_kpi_tile(tile: KpiTile) -> str:
    unit = f" {tile.unit}" if tile.unit else ""
    return f"{tile.label} {tile.value}{unit}".strip()


def _format_vitality_summary(vitality_scores: tuple[VitalityScore, ...]) -> str:
    total_items = len(vitality_scores)
    stale_items = sum(1 for score in vitality_scores if score.freshness_grade == "red")
    missing_fields = sum(1 for score in vitality_scores if score.richness_missing)
    return f"{total_items} items scanned; {stale_items} stale, {missing_fields} missing fields."


def _confidence_label(*, has_ado: bool, has_signals: bool, has_kpis: bool) -> Confidence:
    evidence_count = sum((has_ado, has_signals, has_kpis))
    if evidence_count >= 3:
        return Confidence.HIGH
    if evidence_count == 2:
        return Confidence.MEDIUM
    return Confidence.LOW


def _compute_section_max_risk(items: tuple[WorkItem, ...]) -> RiskLevel:
    """Return the highest risk level among the given work items (BLOCKED > HIGH > ... > UNKNOWN)."""
    if not items:
        return RiskLevel.UNKNOWN
    return max(
        (item.risk_level if item.risk_level is not None else RiskLevel.UNKNOWN for item in items),
        key=lambda level: _RISK_SEVERITY.get(level, 0),
    )


def _metric_observation_to_brief(obs: MetricObservation, *, as_of: datetime) -> MetricEvidenceBrief:
    """FR-SG-15: Convert a MetricObservation to a compact MetricEvidenceBrief."""
    if obs.value_text is not None:
        value_summary = obs.value_text
    elif obs.value_num is not None:
        value_summary = f"{obs.value_num:g}"
    else:
        value_summary = "n/a"
    observed_utc = _ensure_utc(obs.observed_at)
    age_days = (as_of - observed_utc).days
    freshness_label = "fresh" if age_days <= 1 else f"{age_days} day{'s' if age_days != 1 else ''} stale"
    return MetricEvidenceBrief(
        observation_id=obs.observation_id,
        metric_id=obs.metric_id,
        value_summary=value_summary,
        freshness_label=freshness_label,
        quality_state=obs.quality_state.value,
    )


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def build_workstream_evidence_packet(
    workstream_id: str,
    *,
    section_brief: SectionEvidenceBrief,
    all_decisions: tuple[DecisionEntry, ...],
    all_dependencies: tuple[ExternalDependency, ...],
    chronicle_events: tuple[ProgramEvent, ...],
    eta_summary: str | None = None,
    timeline_credibility: float | None = None,
    as_of: datetime,
    top_decisions_limit: int = 5,
    chronicle_days_window: int = 30,
) -> WorkstreamEvidencePacket:
    """FR-SG-19: Assemble a comprehensive evidence packet for a workstream.

    Combines the section evidence brief with decisions, external dependencies,
    and recent chronicle events scoped to the workstream.
    """
    resolved_as_of = _ensure_utc(as_of)
    window_start = resolved_as_of - timedelta(days=chronicle_days_window)

    # Scope decisions to this workstream (workstream_id match or program-level decisions)
    ws_decisions = tuple(
        d for d in all_decisions
        if getattr(d, "workstream_id", None) in (workstream_id, None)
    )
    top_decisions = ws_decisions[:top_decisions_limit]

    # Scope dependencies to this workstream via gates or all deps if unscoped
    ws_deps = tuple(
        dep for dep in all_dependencies
        if not dep.gates or any(workstream_id in gate for gate in dep.gates)
    )

    # Scope chronicle events to this workstream and the rolling window
    ws_chronicle = tuple(
        ev for ev in chronicle_events
        if _ensure_utc(ev.event_date) >= window_start
        and (not ev.linked_dimensions or workstream_id in ev.linked_dimensions)
    )

    return WorkstreamEvidencePacket(
        workstream_id=workstream_id,
        section_brief=section_brief,
        top_decisions=top_decisions,
        open_dependencies=ws_deps,
        chronicle_events=ws_chronicle,
        eta_summary=eta_summary,
        timeline_credibility=timeline_credibility,
        as_of=resolved_as_of,
    )
