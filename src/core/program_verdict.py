from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
from typing import TYPE_CHECKING, Literal

from src.core.exceptions import ConfigError
from src.core.forecast_engine import ETAForecast
from src.core.manifest_writer import hash_jsonable, validate_run_evidence_snapshot
from src.core.models import Confidence, RiskLevel, RunEvidenceSnapshot, Snapshot
from src.core.models_v2 import Milestone, MilestoneAssessment, MilestoneStatus, RiskEntry, RiskImpact, RiskProbability, RiskStatus
from src.core.program_reality import FactAssessment
from src.core.risk_register_engine import HIGH_RISK_SCORE_THRESHOLD, compute_risk_score
from src.core.work_item_states import TERMINAL_WORK_ITEM_STATES

if TYPE_CHECKING:
    from src.core.program_reality import RealityConflict


_RANK = {
    RiskLevel.DONE: 0, RiskLevel.LOW: 1, RiskLevel.UNKNOWN: 2,
    RiskLevel.MEDIUM: 3, RiskLevel.HIGH: 4, RiskLevel.BLOCKED: 5,
}
_UNEVALUATED = (
    "trust_unevaluated", "dependencies_unevaluated", "completion_validation_unevaluated",
    "overrides_unevaluated", "contradictions_unevaluated", "horizon_outcomes_unevaluated",
)


@dataclass(frozen=True, slots=True)
class VerdictReason:
    code: str
    entity_refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LeadershipAsk:
    text: str
    entity_refs: tuple[str, ...]
    missing_fields: tuple[str, ...]
    owner_alias: str | None
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProgramVerdict:
    schema_version: str
    engine_version: str
    policy_version: str
    program_id: str
    snapshot_id: str
    as_of: datetime
    valid_until: datetime | None
    health: RiskLevel
    schedule_risk: RiskLevel
    confidence: Confidence
    completion_phase: str
    reasons: tuple[VerdictReason, ...]
    contradictions: tuple[RealityConflict, ...]
    evidence_gaps: tuple[VerdictReason, ...]
    leadership_asks: tuple[LeadershipAsk, ...]
    source_refs: tuple[str, ...]
    override_refs: tuple[str, ...]
    affected_entity_refs: tuple[str, ...]
    horizon_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProgramVerdictInputs:
    capture: RunEvidenceSnapshot
    snapshot: Snapshot
    forecasts: tuple[ETAForecast, ...]
    risks: tuple[RiskEntry, ...] | None = None
    risk_assessments: tuple[FactAssessment, ...] | None = None
    risk_warnings: tuple[str, ...] = ()
    stale_risk_ids: tuple[str, ...] = ()
    milestones: tuple[Milestone, ...] | None = None
    milestone_assessments: tuple[MilestoneAssessment, ...] | None = None
    milestone_warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProgramVerdictObservation:
    mode: Literal["observe"]
    inputs: ProgramVerdictInputs
    inputs_sha256: str
    verdict: ProgramVerdict


def observe_program_verdict(
    capture: RunEvidenceSnapshot,
    snapshot: Snapshot,
    forecasts: tuple[ETAForecast, ...],
    *,
    risks: tuple[RiskEntry, ...] | None = None,
    risk_assessments: tuple[FactAssessment, ...] | None = None,
    risk_warnings: tuple[str, ...] = (),
    stale_risk_ids: tuple[str, ...] = (),
    milestones: tuple[Milestone, ...] | None = None,
    milestone_assessments: tuple[MilestoneAssessment, ...] | None = None,
    milestone_warnings: tuple[str, ...] = (),
) -> ProgramVerdictObservation:
    forecast_rows = tuple(forecasts)
    if any(type(row.work_item_id) is not int or row.work_item_id <= 0 for row in forecast_rows):
        raise ConfigError("Program verdict forecast identity is invalid.")
    inputs = ProgramVerdictInputs(
        capture, snapshot, tuple(sorted(forecast_rows, key=lambda row: row.work_item_id)),
        None if risks is None else tuple(sorted(risks, key=lambda row: row.id)),
        None if risk_assessments is None else tuple(sorted(risk_assessments, key=hash_jsonable)),
        tuple(sorted(risk_warnings)), tuple(sorted(stale_risk_ids)),
        None if milestones is None else tuple(sorted(milestones, key=lambda row: row.id)),
        None if milestone_assessments is None else tuple(sorted(milestone_assessments, key=hash_jsonable)),
        tuple(sorted(milestone_warnings)),
    )
    verdict = evaluate_program_verdict(inputs)
    return ProgramVerdictObservation("observe", inputs, hash_jsonable(inputs), verdict)


def evaluate_program_verdict(inputs: ProgramVerdictInputs) -> ProgramVerdict:
    capture, snapshot = inputs.capture, inputs.snapshot
    validate_run_evidence_snapshot(capture)
    if (
        capture.schema_version != "run-evidence-capture.v2"
        or hash_jsonable(snapshot) != capture.report_snapshot_hash
    ):
        raise ConfigError("Program verdict capture or report snapshot identity is inconsistent.")
    for clock in (snapshot.generated_at, snapshot.ado_data_as_of, capture.as_of, capture.valid_at):
        if clock.tzinfo is None or clock.utcoffset() is None:
            raise ConfigError("Program verdict requires timezone-aware assessment clocks.")
    as_of = snapshot.ado_data_as_of.astimezone(timezone.utc)
    item_ids = tuple(sorted(item.id for item in snapshot.items))
    dimensions = tuple((row.scorecard_name, row.name) for row in snapshot.scorecards)
    if (
        item_ids != capture.fetched_item_ids or len(set(item_ids)) != len(item_ids)
        or any(type(item_id) is not int or item_id <= 0 for item_id in item_ids)
        or len(set(dimensions)) != len(dimensions)
        or any(not scorecard.strip() or not dimension.strip() for scorecard, dimension in dimensions)
        or any(row.risk_level not in _RANK for row in snapshot.items)
        or any(row.risk not in _RANK for row in snapshot.scorecards)
    ):
        raise ConfigError("Program verdict item or dimension population is inconsistent.")
    items = {item.id: item for item in snapshot.items}
    gaps = [VerdictReason(code) for code in (*capture.limitations, *_UNEVALUATED)]
    seen_forecasts: set[int] = set()
    bound_forecasts: list[ETAForecast] = []
    for forecast in inputs.forecasts:
        if type(forecast.work_item_id) is not int or forecast.work_item_id <= 0:
            raise ConfigError("Program verdict forecast identity is invalid.")
        if forecast.work_item_id in seen_forecasts:
            raise ConfigError("Program verdict forecast identity is duplicated.")
        if (
            isinstance(forecast.slip_probability, bool)
            or not math.isfinite(forecast.slip_probability)
            or not 0 <= forecast.slip_probability <= 1
        ):
            raise ConfigError("Program verdict forecast probability is invalid.")
        seen_forecasts.add(forecast.work_item_id)
        ref = (f"WI:{forecast.work_item_id}",)
        if forecast.work_item_id not in items:
            gaps.append(VerdictReason("forecast_item_uncaptured", ref))
        elif forecast.ado_target_date != items[forecast.work_item_id].target_date:
            gaps.append(VerdictReason("forecast_target_changed", ref))
        else:
            bound_forecasts.append(forecast)

    bound_forecast_ids = {row.work_item_id for row in bound_forecasts}
    reasons: list[VerdictReason] = []
    schedule_risk = RiskLevel.UNKNOWN
    health_levels = [RiskLevel.UNKNOWN]
    for row in snapshot.scorecards:
        ref = (f"SCORECARD:{row.scorecard_name}/{row.name}",)
        health_levels.append(row.risk)
        if row.risk == RiskLevel.UNKNOWN:
            gaps.append(VerdictReason("dimension_assessment_missing", ref))
        elif _RANK[row.risk] > _RANK[RiskLevel.UNKNOWN]:
            reasons.append(VerdictReason("dimension_risk_floor", ref))
    for item in snapshot.items:
        ref = (f"WI:{item.id}",)
        health_levels.append(item.risk_level)
        if _RANK[item.risk_level] > _RANK[RiskLevel.UNKNOWN]:
            reasons.append(VerdictReason("item_risk_floor", ref))
        if item.state.strip().lower() not in TERMINAL_WORK_ITEM_STATES:
            if item.id not in bound_forecast_ids:
                gaps.append(VerdictReason("forecast_missing", ref))
            if item.target_date is not None and item.target_date < as_of.date():
                schedule_risk = RiskLevel.MEDIUM
                reasons.append(VerdictReason("target_overdue", ref))
    if not snapshot.items:
        gaps.append(VerdictReason("items_missing"))
    if not snapshot.scorecards:
        gaps.append(VerdictReason("dimensions_missing"))
    if not bound_forecasts:
        gaps.append(VerdictReason("schedule_evidence_missing"))
    for forecast in bound_forecasts:
        if items[forecast.work_item_id].state.strip().lower() in TERMINAL_WORK_ITEM_STATES:
            continue
        ref = (f"WI:{forecast.work_item_id}",)
        floor = RiskLevel.UNKNOWN
        if forecast.ado_target_date is None:
            gaps.append(VerdictReason("forecast_target_missing", ref))
        elif forecast.slip_probability >= 0.8:
            floor = RiskLevel.HIGH
            reasons.append(VerdictReason("miss_probability_high", ref))
        elif forecast.slip_probability >= 0.5:
            floor = RiskLevel.MEDIUM
            reasons.append(VerdictReason("miss_probability_medium", ref))
        if forecast.p50_date is not None and forecast.p50_date < as_of.date():
            floor = max((floor, RiskLevel.MEDIUM), key=_RANK.__getitem__)
            reasons.append(VerdictReason("forecast_p50_past", ref))
        schedule_risk = max((schedule_risk, floor), key=_RANK.__getitem__)
    asks: list[LeadershipAsk] = []
    gaps.append(VerdictReason("risk_register_coverage_unqualified"))
    if inputs.risks is None:
        gaps.append(VerdictReason("risk_register_asks_unevaluated"))
    if inputs.risk_warnings:
        gaps.append(VerdictReason("risk_input_degraded"))
    risk_ids: set[str] = set()
    for risk in inputs.risks or ():
        if not risk.id.strip() or risk.id in risk_ids or risk.program_id != capture.program_id:
            raise ConfigError("Program verdict risk identity is inconsistent.")
        risk_ids.add(risk.id)
        ref = (f"RISK:{risk.id}",)
        matching = tuple(
            row for row in inputs.risk_assessments or ()
            if isinstance(row.record, RiskEntry)
            and (row.record.program_id, row.record.id) == (risk.program_id, risk.id)
        )
        assessment = matching[0] if len(matching) == 1 and matching[0].record == risk else None
        if assessment is None:
            gaps.append(VerdictReason("risk_assessment_unavailable", ref))
        else:
            for limited, code in (
                (assessment.disputed, "risk_assessment_disputed"),
                (assessment.stale, "risk_assessment_stale"),
                (assessment.provisional_inputs, "risk_assessment_provisional"),
            ):
                if limited:
                    gaps.append(VerdictReason(code, ref))
        if risk.id in inputs.stale_risk_ids:
            gaps.append(VerdictReason("risk_review_stale", ref))
        if risk.kind != "strategic" or risk.status not in {RiskStatus.OPEN, RiskStatus.ESCALATED}:
            continue
        if risk.probability == RiskProbability.UNASSESSED or risk.impact == RiskImpact.UNASSESSED:
            gaps.append(VerdictReason("risk_score_unassessed", ref))
            continue
        if compute_risk_score(risk) < HIGH_RISK_SCORE_THRESHOLD:
            continue
        health_levels.append(RiskLevel.HIGH)
        reasons.append(VerdictReason("risk_register_high_floor", ref))
        missing = tuple(
            field for field, absent in (
                ("owner", not risk.owner_alias.strip()),
                ("mitigation_eta", risk.mitigation_due_date is None),
            ) if absent
        )
        if missing:
            text = " and ".join("owner" if field == "owner" else "mitigation ETA" for field in missing)
            asks.append(LeadershipAsk(
                text=f"Assign {text} for risk {risk.id}.", entity_refs=ref,
                missing_fields=missing, owner_alias=risk.owner_alias.strip() or None,
                evidence_refs=tuple(sorted({
                    *((f"FACT:{assessment.fact_id}",) if assessment is not None and assessment.fact_id else ()),
                    *(assessment.evidence if assessment is not None else ()),
                })),
            ))
    if any(row.record not in (inputs.risks or ()) for row in inputs.risk_assessments or ()):
        gaps.append(VerdictReason("risk_assessment_unbound"))
    gaps.append(VerdictReason("milestone_inputs_unqualified"))
    if inputs.milestones is None:
        gaps.append(VerdictReason("milestones_unevaluated"))
    if inputs.milestone_warnings:
        gaps.append(VerdictReason("milestone_input_degraded"))
    milestone_ids: set[str] = set()
    for milestone in inputs.milestones or ():
        if (
            not milestone.id.strip() or milestone.id in milestone_ids
            or milestone.program_id != capture.program_id
        ):
            raise ConfigError("Program verdict milestone identity is inconsistent.")
        milestone_ids.add(milestone.id)
        ref = (f"MILESTONE:{milestone.id}",)
        matching_milestones = tuple(
            row for row in inputs.milestone_assessments or () if row.milestone_id == milestone.id
        )
        milestone_assessment = matching_milestones[0] if len(matching_milestones) == 1 else None
        status = milestone_assessment.computed_health if milestone_assessment is not None else None
        if milestone_assessment is None:
            gaps.append(VerdictReason("milestone_assessment_unavailable", ref))
        else:
            if milestone_assessment.coverage_gap:
                gaps.append(VerdictReason("milestone_evidence_incomplete", ref))
            if status == MilestoneStatus.UNKNOWN:
                gaps.append(VerdictReason("milestone_assessment_unknown", ref))
        milestone_floor = RiskLevel.UNKNOWN
        if status == MilestoneStatus.MISSED:
            milestone_floor = RiskLevel.HIGH
            reasons.append(VerdictReason("milestone_missed", ref))
        elif status == MilestoneStatus.AT_RISK:
            milestone_floor = RiskLevel.MEDIUM
            reasons.append(VerdictReason("milestone_at_risk", ref))
        if (
            milestone.target_date < as_of.date()
            and status not in {MilestoneStatus.COMPLETED, MilestoneStatus.DEFERRED}
            and not (status is None and milestone.status == MilestoneStatus.DEFERRED)
        ):
            milestone_floor = max((milestone_floor, RiskLevel.MEDIUM), key=_RANK.__getitem__)
            reasons.append(VerdictReason("milestone_target_overdue", ref))
        schedule_risk = max((schedule_risk, milestone_floor), key=_RANK.__getitem__)
    if any(row.milestone_id not in milestone_ids for row in inputs.milestone_assessments or ()):
        gaps.append(VerdictReason("milestone_assessment_unbound"))
    source_refs = tuple(sorted({
        *(f"WI:{item_id}" for item_id in item_ids),
        *(f"FACT_REVISION:{binding.fact_revision_id}" for binding in capture.binding_declarations),
        *(f"RISK:{risk_id}" for risk_id in risk_ids),
        *(f"MILESTONE:{milestone_id}" for milestone_id in milestone_ids),
    }))
    return ProgramVerdict(
        schema_version="program-verdict.v1", engine_version="observe.v3",
        policy_version="observe-conservative.v3", program_id=capture.program_id,
        snapshot_id=capture.sha256, as_of=as_of, valid_until=None,
        health=max((*health_levels, schedule_risk), key=_RANK.__getitem__),
        schedule_risk=schedule_risk, confidence=Confidence.NONE, completion_phase="unknown",
        reasons=tuple(sorted(reasons, key=lambda row: (row.code, row.entity_refs))),
        contradictions=(), evidence_gaps=tuple(sorted(gaps, key=lambda row: (row.code, row.entity_refs))),
        leadership_asks=tuple(sorted(asks, key=lambda row: row.entity_refs)), source_refs=source_refs, override_refs=(),
        affected_entity_refs=tuple(sorted({
            *source_refs, *(f"SCORECARD:{name}/{dimension}" for name, dimension in dimensions),
        })),
        horizon_refs=(),
    )
