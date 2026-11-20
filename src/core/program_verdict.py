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
from src.core.models import Confidence, DimensionRisk, RiskLevel, RunEvidenceSnapshot, Snapshot, WorkItem
from src.core.models_v2 import Milestone, MilestoneAssessment, MilestoneStatus, RiskEntry, RiskImpact, RiskProbability, RiskStatus
from src.core.program_reality import FactAssessment, RealityConflict
from src.core.risk_register_engine import HIGH_RISK_SCORE_THRESHOLD, compute_risk_score
from src.core.work_item_states import TERMINAL_WORK_ITEM_STATES
from src.core.workspace_paths import is_bare_identifier

if TYPE_CHECKING:
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


def _extract_observation_candidate(
    program_id: str,
    path: Path,
) -> tuple[datetime, Path, dict] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ConfigError("Program verdict manifest is invalid or unreadable.") from None
    if not isinstance(payload, dict):
        raise ConfigError("Program verdict manifest is invalid.")
    metadata = payload.get("metadata")
    observation = metadata.get("program_verdict_observation") if isinstance(metadata, dict) else None
    if observation is None:
        return None
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
    return clock.astimezone(timezone.utc), path, verdict


def _discover_confirmed_issue_keys(
    program_id: str,
    *,
    programs_root: Path,
    archive_root: Path | None,
) -> tuple[set[tuple[str, int]], list[tuple[datetime, Path, dict]]]:
    confirmed_keys: set[tuple[str, int]] = set()
    archive_candidates: list[tuple[datetime, Path, dict]] = []
    candidate_roots = []
    for base in (archive_root, programs_root.parent / "archive", programs_root / program_id / "archive"):
        if base is not None and base not in candidate_roots and base.is_dir():
            candidate_roots.append(base)
    seen_manifests: set[Path] = set()
    for base in candidate_roots:
        resolved_base = base.resolve()
        for index_path in sorted(base.glob("*/index.json")):
            edition = index_path.parent.name
            try:
                index_payload = json.loads(index_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            raw_issues = index_payload.get("issues") if isinstance(index_payload, dict) else None
            if not isinstance(raw_issues, list):
                continue
            for entry in raw_issues:
                if not isinstance(entry, dict) or entry.get("kind") != "confirmed":
                    continue
                issue_num = entry.get("issue_number")
                if type(issue_num) is not int:
                    continue
                confirmed_keys.add((edition, issue_num))
                manifest_path = (index_path.parent / "manifests" / f"issue_{issue_num:03d}.manifest.json").resolve()
                if manifest_path.is_file() and manifest_path not in seen_manifests:
                    try:
                        manifest_path.relative_to(resolved_base)
                    except ValueError:
                        continue
                    seen_manifests.add(manifest_path)
                    candidate = _extract_observation_candidate(program_id, manifest_path)
                    if candidate is not None:
                        archive_candidates.append(candidate)
    return confirmed_keys, archive_candidates


def load_latest_program_verdict(
    program_id: str,
    *,
    programs_root: Path,
    archive_root: Path | None = None,
    confirmed_only: bool = False,
) -> PublishedProgramVerdict | None:
    """Load the newest retained snapshot-bound verdict without draft shadowing."""
    if not is_bare_identifier(program_id):
        raise ConfigError("Program verdict program identity is invalid.")
    selected_root = programs_root.resolve()
    try:
        program_root = (selected_root / program_id).resolve().relative_to(selected_root)
    except ValueError:
        raise ConfigError("Program verdict program root is invalid.") from None
    confirmed_keys, archive_candidates = _discover_confirmed_issue_keys(
        program_id, programs_root=selected_root, archive_root=archive_root,
    )
    publications = selected_root / program_root / "publications"
    pub_confirmed: list[tuple[datetime, Path, dict]] = []
    pub_all: list[tuple[datetime, Path, dict]] = []
    if publications.exists():
        publication_root = publications.resolve()
        for path in publications.rglob("*.manifest.json"):
            try:
                rel = path.resolve().relative_to(publication_root)
            except (OSError, ValueError):
                raise ConfigError("Program verdict manifest path is invalid.") from None
            candidate = _extract_observation_candidate(program_id, path)
            if candidate is None:
                continue
            pub_all.append(candidate)
            if len(rel.parts) >= 2 and rel.parts[1].startswith("issue_"):
                suffix = rel.parts[1].removeprefix("issue_")
                if suffix.isdigit() and (rel.parts[0], int(suffix)) in confirmed_keys:
                    pub_confirmed.append(candidate)

    if confirmed_only or confirmed_keys:
        pool = archive_candidates or pub_confirmed
        if archive_candidates and pub_confirmed:
            pool = archive_candidates + pub_confirmed
        if not pool:
            return None
        _, path, verdict = max(pool, key=lambda row: (row[0], str(row[1])))
        return _published_verdict_from_payload(program_id, path, verdict)
    if not pub_all:
        return None
    _, path, verdict = max(pub_all, key=lambda row: (row[0], str(row[1])))
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
    verdict_health = max((*health_levels, schedule_risk), key=_RANK.__getitem__)
    contradictions = _detect_verdict_contradictions(
        scorecards=snapshot.scorecards,
        verdict_health=verdict_health,
        reasons=reasons,
    )
    source_refs = tuple(sorted({
        *(f"WI:{item_id}" for item_id in item_ids),
        *(f"FACT_REVISION:{binding.fact_revision_id}" for binding in capture.binding_declarations),
        *(f"RISK:{risk_id}" for risk_id in risk_ids),
        *(f"MILESTONE:{milestone_id}" for milestone_id in milestone_ids),
    }))
    result = ProgramVerdict(
        schema_version="program-verdict.v1", engine_version="observe.v3",
        policy_version="observe-conservative.v3", program_id=capture.program_id,
        snapshot_id=capture.sha256, as_of=as_of, valid_until=None,
        health=verdict_health,
        schedule_risk=schedule_risk, confidence=Confidence.NONE, completion_phase="unknown",
        reasons=tuple(sorted(reasons, key=lambda row: (row.code, row.entity_refs))),
        contradictions=contradictions,
        evidence_gaps=tuple(sorted(gaps, key=lambda row: (row.code, row.entity_refs))),
        leadership_asks=tuple(sorted(asks, key=lambda row: row.entity_refs)), source_refs=source_refs, override_refs=(),
        affected_entity_refs=tuple(sorted({
            *source_refs, *(f"SCORECARD:{name}/{dimension}" for name, dimension in dimensions),
        })),
        horizon_refs=(),
    )
    verify_program_verdict_oracle(inputs, result)
    return result


def _detect_verdict_contradictions(
    *,
    scorecards: tuple[object, ...],
    verdict_health: RiskLevel,
    reasons: list[VerdictReason] | tuple[VerdictReason, ...],
) -> tuple[RealityConflict, ...]:
    adverse_refs = tuple(
        sorted(
            {
                ref
                for reason in reasons
                if reason.code != "dimension_risk_floor"
                for ref in reason.entity_refs
            }
        )
    )
    if not adverse_refs or _RANK[verdict_health] <= _RANK[RiskLevel.UNKNOWN]:
        return ()
    conflicts: list[RealityConflict] = []
    for row in scorecards:
        row_risk = getattr(row, "risk", RiskLevel.UNKNOWN)
        if row_risk not in {RiskLevel.LOW, RiskLevel.DONE}:
            continue
        if _RANK[row_risk] >= _RANK[verdict_health]:
            continue
        scorecard_name = getattr(row, "scorecard_name", "Scorecard")
        dim_name = getattr(row, "name", "unknown")
        dim_ref = f"SCORECARD:{scorecard_name}/{dim_name}"
        conflicts.append(
            RealityConflict(
                conflict_id=f"verdict-contradiction:{scorecard_name}/{dim_name}",
                entity_refs=(dim_ref, *adverse_refs),
                family="verdict.contradiction",
                open=True,
                description=(
                    f"Optimistic scorecard dimension {scorecard_name}/{dim_name} ({row_risk.value}) "
                    f"conflicts with conservative program verdict floor ({verdict_health.value})."
                ),
                winning_source="program_verdict.conservative_floor",
                losing_source=dim_ref,
                winning_value=verdict_health.value,
                losing_value=row_risk.value,
                resolution="conservative_max_floor",
            )
        )
    return tuple(sorted(conflicts, key=lambda item: item.conflict_id))


def verify_program_verdict_oracle(inputs: ProgramVerdictInputs, verdict: ProgramVerdict) -> None:
    """Correctness oracle: verify conservative floors, contradictions, and identity invariants."""
    expected_as_of = inputs.snapshot.ado_data_as_of.astimezone(timezone.utc)
    if (
        verdict.program_id != inputs.capture.program_id
        or verdict.snapshot_id != inputs.capture.sha256
        or verdict.as_of != expected_as_of
    ):
        raise ConfigError("Program verdict correctness oracle violation: verdict identity or clock mismatch.")

    expected_floor = RiskLevel.UNKNOWN
    for row in inputs.snapshot.scorecards:
        if _RANK[row.risk] > _RANK[expected_floor]:
            expected_floor = row.risk
    for item in inputs.snapshot.items:
        if _RANK[item.risk_level] > _RANK[expected_floor]:
            expected_floor = item.risk_level
        if (
            item.state.strip().lower() not in TERMINAL_WORK_ITEM_STATES
            and item.target_date is not None
            and item.target_date < expected_as_of.date()
            and _RANK[RiskLevel.MEDIUM] > _RANK[verdict.schedule_risk]
        ):
            raise ConfigError("Program verdict correctness oracle violation: overdue item missed schedule floor.")
    for risk in inputs.risks or ():
        if (
            risk.kind == "strategic"
            and risk.status in {RiskStatus.OPEN, RiskStatus.ESCALATED}
            and risk.probability != RiskProbability.UNASSESSED
            and risk.impact != RiskImpact.UNASSESSED
            and compute_risk_score(risk) >= HIGH_RISK_SCORE_THRESHOLD
            and _RANK[RiskLevel.HIGH] > _RANK[verdict.health]
        ):
            raise ConfigError("Program verdict correctness oracle violation: high strategic risk lowered.")

    for ms in inputs.milestones or ():
        matching = [a for a in (inputs.milestone_assessments or ()) if a.milestone_id == ms.id]
        ma = matching[0] if len(matching) == 1 else None
        ms_status = ma.computed_health if ma is not None else None
        if ms_status == MilestoneStatus.MISSED and _RANK[RiskLevel.HIGH] > _RANK[verdict.schedule_risk]:
            raise ConfigError("Program verdict correctness oracle violation: missed milestone lowered schedule risk.")
        if ms_status == MilestoneStatus.AT_RISK and _RANK[RiskLevel.MEDIUM] > _RANK[verdict.schedule_risk]:
            raise ConfigError("Program verdict correctness oracle violation: at-risk milestone lowered schedule risk.")

    if _RANK[verdict.health] < _RANK[expected_floor] or _RANK[verdict.health] < _RANK[verdict.schedule_risk]:
        raise ConfigError("Program verdict correctness oracle violation: health below conservative risk floor.")

    adverse_refs = [r for r in verdict.reasons if r.code != "dimension_risk_floor"]
    optimistic_dims = [
        f"verdict-contradiction:{row.scorecard_name}/{row.name}"
        for row in inputs.snapshot.scorecards
        if row.risk in {RiskLevel.LOW, RiskLevel.DONE} and _RANK[row.risk] < _RANK[verdict.health]
    ]
    if adverse_refs and optimistic_dims:
        recorded_ids = {c.conflict_id for c in verdict.contradictions}
        missing = sorted(set(optimistic_dims) - recorded_ids)
        if missing:
            raise ConfigError(
                f"Program verdict correctness oracle violation: silent contradictions {missing}."
            )


@dataclass(frozen=True, slots=True)
class AuthoritativeRiskAssessment:
    overall_risk: RiskLevel
    schedule_risk: RiskLevel
    contradictions: tuple[RealityConflict, ...]
    reason_codes: tuple[str, ...]


def resolve_authoritative_program_risk(
    *,
    scorecards: tuple[DimensionRisk | object, ...] = (),
    items: tuple[WorkItem | object, ...] = (),
    forecasts: tuple[ETAForecast, ...] = (),
    risks: tuple[RiskEntry, ...] = (),
    milestones: tuple[Milestone, ...] = (),
    milestone_assessments: tuple[MilestoneAssessment, ...] = (),
    as_of: datetime | None = None,
) -> AuthoritativeRiskAssessment:
    """Compute conservative RiskLevel floor identically to evaluate_program_verdict for render paths."""
    reasons: list[VerdictReason] = []
    levels = [RiskLevel.UNKNOWN]
    schedule_risk = RiskLevel.UNKNOWN

    for row in scorecards:
        raw_risk = getattr(row, "risk", RiskLevel.UNKNOWN)
        row_risk = RiskLevel.UNKNOWN if raw_risk == RiskLevel.BLOCKED else raw_risk
        levels.append(row_risk)
        if _RANK[row_risk] > _RANK[RiskLevel.UNKNOWN]:
            reasons.append(VerdictReason("dimension_risk_floor", (f"SCORECARD:{getattr(row, 'scorecard_name', 'Scorecard')}/{getattr(row, 'name', '')}",)))

    item_map = {}
    as_of_date = as_of.date() if as_of is not None else None
    for item in items:
        item_id = getattr(item, "id", None)
        item_risk = getattr(item, "risk_level", RiskLevel.UNKNOWN)
        item_state = str(getattr(item, "state", "")).strip().lower()
        target_date = getattr(item, "target_date", None)
        if item_id is not None:
            item_map[item_id] = item
        levels.append(item_risk)
        if _RANK[item_risk] > _RANK[RiskLevel.UNKNOWN]:
            reasons.append(VerdictReason("item_risk_floor", (f"WI:{item_id}",)))
        if as_of_date is not None and item_state not in TERMINAL_WORK_ITEM_STATES and target_date is not None and target_date < as_of_date:
            schedule_risk = max((schedule_risk, RiskLevel.MEDIUM), key=_RANK.__getitem__)
            reasons.append(VerdictReason("target_overdue", (f"WI:{item_id}",)))

    for fc in forecasts:
        item = item_map.get(fc.work_item_id)
        if item is not None and str(getattr(item, "state", "")).strip().lower() in TERMINAL_WORK_ITEM_STATES:
            continue
        if fc.ado_target_date is not None:
            if fc.slip_probability >= 0.8:
                schedule_risk = max((schedule_risk, RiskLevel.HIGH), key=_RANK.__getitem__)
                reasons.append(VerdictReason("miss_probability_high", (f"WI:{fc.work_item_id}",)))
            elif fc.slip_probability >= 0.5:
                schedule_risk = max((schedule_risk, RiskLevel.MEDIUM), key=_RANK.__getitem__)
                reasons.append(VerdictReason("miss_probability_medium", (f"WI:{fc.work_item_id}",)))
        if as_of_date is not None and fc.p50_date is not None and fc.p50_date < as_of_date:
            schedule_risk = max((schedule_risk, RiskLevel.MEDIUM), key=_RANK.__getitem__)
            reasons.append(VerdictReason("forecast_p50_past", (f"WI:{fc.work_item_id}",)))

    for risk in risks:
        if risk.kind != "strategic" or risk.status not in {RiskStatus.OPEN, RiskStatus.ESCALATED}:
            continue
        if risk.probability == RiskProbability.UNASSESSED or risk.impact == RiskImpact.UNASSESSED:
            continue
        if compute_risk_score(risk) >= HIGH_RISK_SCORE_THRESHOLD:
            levels.append(RiskLevel.HIGH)
            reasons.append(VerdictReason("risk_register_high_floor", (f"RISK:{risk.id}",)))

    for ms in milestones:
        matching = [a for a in milestone_assessments if a.milestone_id == ms.id]
        ma = matching[0] if len(matching) == 1 else None
        status = ma.computed_health if ma is not None else None
        if status == MilestoneStatus.MISSED:
            schedule_risk = max((schedule_risk, RiskLevel.HIGH), key=_RANK.__getitem__)
            reasons.append(VerdictReason("milestone_missed", (f"MILESTONE:{ms.id}",)))
        elif status == MilestoneStatus.AT_RISK:
            schedule_risk = max((schedule_risk, RiskLevel.MEDIUM), key=_RANK.__getitem__)
            reasons.append(VerdictReason("milestone_at_risk", (f"MILESTONE:{ms.id}",)))
        if (
            as_of_date is not None
            and ms.target_date < as_of_date
            and status not in {MilestoneStatus.COMPLETED, MilestoneStatus.DEFERRED}
            and not (status is None and ms.status == MilestoneStatus.DEFERRED)
        ):
            schedule_risk = max((schedule_risk, RiskLevel.MEDIUM), key=_RANK.__getitem__)
            reasons.append(VerdictReason("milestone_target_overdue", (f"MILESTONE:{ms.id}",)))

    overall_sc = RiskLevel.UNKNOWN
    has_unknown_sc = False
    for row in scorecards:
        raw_risk = getattr(row, "risk", RiskLevel.UNKNOWN)
        row_risk = RiskLevel.UNKNOWN if raw_risk == RiskLevel.BLOCKED else raw_risk
        if row_risk == RiskLevel.UNKNOWN:
            has_unknown_sc = True
        if _RANK[row_risk] > _RANK[overall_sc]:
            overall_sc = row_risk
    if scorecards and all(getattr(row, "risk", RiskLevel.UNKNOWN) in {RiskLevel.LOW, RiskLevel.DONE} for row in scorecards):
        overall_sc = max((getattr(row, "risk", RiskLevel.UNKNOWN) for row in scorecards), key=_RANK.__getitem__)
    elif has_unknown_sc and _RANK[overall_sc] < _RANK[RiskLevel.UNKNOWN]:
        overall_sc = RiskLevel.UNKNOWN
    adverse_max = max((*levels, schedule_risk), key=_RANK.__getitem__)
    overall = adverse_max if _RANK[adverse_max] >= _RANK[RiskLevel.MEDIUM] else overall_sc
    conflicts = _detect_verdict_contradictions(scorecards=scorecards, verdict_health=overall, reasons=reasons)
    return AuthoritativeRiskAssessment(
        overall_risk=overall,
        schedule_risk=schedule_risk,
        contradictions=conflicts,
        reason_codes=tuple(sorted({r.code for r in reasons})),
    )


def render_published_verdict_line(verdict: PublishedProgramVerdict) -> str:
    """Format a deterministic, ban-list-safe one-line summary of a PublishedProgramVerdict."""
    return (
        f"Program Verdict ({verdict.program_id}, snapshot {verdict.snapshot_id}, as_of {verdict.as_of.isoformat()}): "
        f"health={verdict.health.value.upper()} | schedule_risk={verdict.schedule_risk.value.upper()} | "
        f"reasons={len(verdict.reason_codes)} | gaps={len(verdict.evidence_gap_codes)}"
    )


def assert_cross_surface_verdict_parity(
    *,
    published: PublishedProgramVerdict,
    report_risk: RiskLevel,
    cockpit_overall_risk: str,
    brief_verdict: PublishedProgramVerdict | None,
    escalate_verdict: PublishedProgramVerdict | None,
    review_verdict: PublishedProgramVerdict | None,
) -> None:
    """Cross-surface oracle: assert report, cockpit, brief, escalate, and review agree on PublishedProgramVerdict."""
    expected_cockpit = {
        "blocked": "red",
        "high": "red",
        "medium": "yellow",
        "low": "green",
        "done": "green",
        "unknown": "unknown",
    }[published.health.value]
    if report_risk != published.health:
        raise ConfigError(
            f"Cross-surface verdict conflict: report_risk={report_risk.value} != published={published.health.value}."
        )
    if cockpit_overall_risk != expected_cockpit:
        raise ConfigError(
            f"Cross-surface verdict conflict: cockpit={cockpit_overall_risk} != expected={expected_cockpit}."
        )
    for surface, candidate in (
        ("brief", brief_verdict),
        ("escalate", escalate_verdict),
        ("review", review_verdict),
    ):
        if candidate is None or candidate.snapshot_id != published.snapshot_id or candidate.health != published.health:
            raise ConfigError(f"Cross-surface verdict conflict on {surface}: expected {published.snapshot_id}/{published.health.value}.")
