from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from src.core.exceptions import ConfigError
from src.core.forecast_engine import ETAForecast
from src.core.manifest_writer import hash_jsonable, validate_run_evidence_snapshot
from src.core.models import Confidence, RiskLevel, RunEvidenceSnapshot, Snapshot
from src.core.models_v2 import Milestone, MilestoneAssessment, MilestoneStatus, RiskEntry, RiskImpact, RiskProbability, RiskStatus
from src.core.program_reality import FactAssessment
from src.core.risk_register_engine import HIGH_RISK_SCORE_THRESHOLD, compute_risk_score
from src.core.work_item_states import TERMINAL_WORK_ITEM_STATES
from src.core.workspace_paths import is_bare_identifier

if TYPE_CHECKING:
    from src.core.program_reality import RealityConflict
    from src.core.view_models import HealthSummary


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
class PublishedProgramVerdict:
    program_id: str
    snapshot_id: str
    as_of: datetime
    health: RiskLevel
    schedule_risk: RiskLevel
    confidence: Confidence
    completion_phase: str
    reason_codes: tuple[str, ...]
    evidence_gap_codes: tuple[str, ...]
    leadership_ask_count: int
    source_refs: tuple[str, ...]
    manifest_path: Path


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
    verdict_sha256: str


@dataclass(frozen=True, slots=True)
class ReportVerdictComparison:
    schema_version: str
    mode: Literal["observe"]
    capture_sha256: str
    observation_inputs_sha256: str
    report_risk: RiskLevel
    report_health_state: str
    verdict_risk: RiskLevel
    risk_values_equal: bool


def load_latest_program_verdict(
    program_id: str, *, programs_root: Path
) -> PublishedProgramVerdict | None:
    """Load the newest retained snapshot-bound verdict without older fallback."""
    if not is_bare_identifier(program_id):
        raise ConfigError("Program verdict program identity is invalid.")
    selected_root = programs_root.resolve()
    try:
        program_root = (selected_root / program_id).resolve().relative_to(selected_root)
    except ValueError:
        raise ConfigError("Program verdict program root is invalid.") from None
    publications = selected_root / program_root / "publications"
    if not publications.exists():
        return None
    candidates: list[tuple[datetime, Path, dict]] = []
    publication_root = publications.resolve()
    for path in publications.rglob("*.manifest.json"):
        try:
            path.resolve().relative_to(publication_root)
        except (OSError, ValueError):
            raise ConfigError("Program verdict manifest path is invalid.") from None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise ConfigError("Program verdict manifest is invalid or unreadable.") from None
        if not isinstance(payload, dict):
            raise ConfigError("Program verdict manifest is invalid.")
        metadata = payload.get("metadata")
        observation = metadata.get("program_verdict_observation") if isinstance(metadata, dict) else None
        if observation is None:
            continue
        if not isinstance(observation, dict) or observation.get("mode") != "observe":
            raise ConfigError("Program verdict manifest observation is invalid.")
        verdict = observation.get("verdict")
        if not isinstance(verdict, dict) or verdict.get("program_id") != program_id:
            raise ConfigError("Program verdict manifest observation identity is invalid.")
        raw_clock = payload.get("started_at")
        if not isinstance(raw_clock, str):
            raise ConfigError("Program verdict manifest clock is unavailable.")
        try:
            clock = datetime.fromisoformat(raw_clock.replace("Z", "+00:00"))
        except ValueError:
            raise ConfigError("Program verdict manifest clock is invalid.") from None
        if clock.tzinfo is None or clock.utcoffset() is None:
            raise ConfigError("Program verdict manifest clock must be timezone-aware.")
        candidates.append((clock.astimezone(timezone.utc), path, verdict))
    if not candidates:
        return None
    _, path, verdict = max(candidates, key=lambda row: (row[0], str(row[1])))
    return _published_verdict_from_payload(program_id, path, verdict)


def _published_verdict_from_payload(
    program_id: str, path: Path, verdict: dict
) -> PublishedProgramVerdict:
    try:
        if verdict.get("schema_version") != "program-verdict.v1":
            raise ValueError("schema")
        raw_snapshot_id = verdict["snapshot_id"]
        raw_as_of = verdict["as_of"]
        raw_completion_phase = verdict.get("completion_phase", "unknown")
        raw_confidence = verdict.get("confidence", "none")
        if (
            not isinstance(raw_snapshot_id, str) or not raw_snapshot_id.strip()
            or not isinstance(raw_as_of, str)
            or not isinstance(raw_completion_phase, str) or not raw_completion_phase.strip()
            or not isinstance(raw_confidence, str)
        ):
            raise ValueError("shape")
        as_of = datetime.fromisoformat(raw_as_of.replace("Z", "+00:00"))
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("clock")
        reason_codes = _manifest_reason_codes(verdict.get("reasons"))
        evidence_gap_codes = _manifest_reason_codes(verdict.get("evidence_gaps"))
        asks = verdict.get("leadership_asks")
        refs = verdict.get("source_refs")
        if (
            not isinstance(asks, list)
            or any(not isinstance(row, dict) for row in asks)
            or not isinstance(refs, list)
            or any(not isinstance(ref, str) or not ref.strip() for ref in refs)
        ):
            raise ValueError("shape")
        return PublishedProgramVerdict(
            program_id=program_id,
            snapshot_id=raw_snapshot_id,
            as_of=as_of.astimezone(timezone.utc),
            health=RiskLevel(verdict["health"]),
            schedule_risk=RiskLevel(verdict["schedule_risk"]),
            confidence=Confidence(raw_confidence),
            completion_phase=raw_completion_phase,
            reason_codes=reason_codes,
            evidence_gap_codes=evidence_gap_codes,
            leadership_ask_count=len(asks),
            source_refs=tuple(refs),
            manifest_path=path,
        )
    except (KeyError, TypeError, ValueError):
        raise ConfigError("Program verdict manifest payload is invalid.") from None


def _manifest_reason_codes(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError("reason shape")
    codes: list[str] = []
    for row in value:
        if not isinstance(row, dict):
            raise ValueError("reason row")
        code = row.get("code")
        if not isinstance(code, str) or not code.strip():
            raise ValueError("reason code")
        codes.append(code)
    return tuple(sorted(codes))


def compare_report_health(
    health: HealthSummary, observation: ProgramVerdictObservation,
) -> ReportVerdictComparison:
    return ReportVerdictComparison(
        schema_version="report-verdict-comparison.v1",
        mode="observe",
        capture_sha256=observation.verdict.snapshot_id,
        observation_inputs_sha256=observation.inputs_sha256,
        report_risk=health.overall_risk,
        report_health_state=health.health_state,
        verdict_risk=observation.verdict.health,
        risk_values_equal=health.overall_risk == observation.verdict.health,
    )


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
    return ProgramVerdictObservation(
        "observe", inputs, hash_jsonable(inputs), verdict, hash_jsonable(verdict),
    )


def validate_retained_program_verdict(
    payload: object,
    *,
    capture: RunEvidenceSnapshot,
    report_snapshot_hash: str,
) -> str:
    if not isinstance(payload, Mapping):
        raise ConfigError("The retained program verdict observation is invalid.")
    mode = payload.get("mode")
    inputs = payload.get("inputs")
    verdict = payload.get("verdict")
    inputs_sha256 = payload.get("inputs_sha256")
    verdict_sha256 = payload.get("verdict_sha256")
    if (
        mode != "observe"
        or not isinstance(inputs, Mapping)
        or not isinstance(verdict, Mapping)
        or not isinstance(inputs_sha256, str)
        or not isinstance(verdict_sha256, str)
    ):
        raise ConfigError("The retained program verdict observation is invalid.")
    retained_capture = inputs.get("capture")
    retained_snapshot = inputs.get("snapshot")
    if (
        not isinstance(retained_capture, Mapping)
        or not isinstance(retained_snapshot, Mapping)
        or hash_jsonable(retained_capture) != hash_jsonable(capture)
        or hash_jsonable(retained_snapshot) != report_snapshot_hash
        or report_snapshot_hash != capture.report_snapshot_hash
    ):
        raise ConfigError("The retained program verdict observation capture binding is invalid.")
    if hash_jsonable(inputs) != inputs_sha256 or hash_jsonable(verdict) != verdict_sha256:
        raise ConfigError("The retained program verdict observation content binding is invalid.")
    if (
        verdict.get("schema_version") != "program-verdict.v1"
        or verdict.get("program_id") != capture.program_id
        or verdict.get("snapshot_id") != capture.sha256
    ):
        raise ConfigError("The retained program verdict observation identity is invalid.")
    return inputs_sha256


def validate_retained_program_verdict_comparison(
    payload: object,
    *,
    capture_sha256: str,
    observation_inputs_sha256: str,
) -> None:
    if not isinstance(payload, Mapping):
        raise ConfigError("The retained report verdict comparison is invalid.")
    report_risk = payload.get("report_risk")
    verdict_risk = payload.get("verdict_risk")
    risk_values_equal = payload.get("risk_values_equal")
    if (
        payload.get("schema_version") != "report-verdict-comparison.v1"
        or payload.get("mode") != "observe"
        or payload.get("capture_sha256") != capture_sha256
        or payload.get("observation_inputs_sha256") != observation_inputs_sha256
        or not isinstance(report_risk, str)
        or not isinstance(verdict_risk, str)
        or not isinstance(risk_values_equal, bool)
        or risk_values_equal is not (report_risk == verdict_risk)
    ):
        raise ConfigError("The retained report verdict comparison binding is invalid.")


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
