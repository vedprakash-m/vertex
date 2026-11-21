from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
import math
from typing import Any


_REQUIRED_SLICES = ("status", "dependency", "meeting-close")
_REQUIRED_EXTERNAL_EVIDENCE = (
    "d9_decision",
    "owner",
    "blind_audit_protocol",
    "live_cycle",
    "real_program",
)


class QualificationStatus(str, Enum):
    QUALIFIED = "qualified"
    NOT_QUALIFIED = "not_qualified"


@dataclass(frozen=True, slots=True)
class VerticalSliceResult:
    kind: str
    factual_non_inferiority: bool
    missing_fact_ids: tuple[str, ...]
    contradicted_fact_ids: tuple[str, ...]
    unsupported_fact_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class VerticalArtifactReport:
    schema_version: str
    slices: tuple[VerticalSliceResult, ...]
    cost_non_inferiority: bool
    review_time_non_inferiority: bool
    factual_and_operational_checks_pass: bool
    missing_external_evidence: tuple[str, ...]
    schema_errors: tuple[str, ...]
    qualification_status: QualificationStatus


def evaluate_vertical_artifact_protocol(payload: dict[str, Any]) -> VerticalArtifactReport:
    errors: list[str] = []
    baseline = _object(payload.get("baseline"), "baseline", errors)
    candidate = _object(payload.get("candidate"), "candidate", errors)
    baseline_slices = _slice_index(baseline, "baseline", errors)
    candidate_slices = _slice_index(candidate, "candidate", errors)
    slices: list[VerticalSliceResult] = []
    for kind in _REQUIRED_SLICES:
        if kind not in baseline_slices:
            errors.append(f"missing baseline slice: {kind}")
            baseline_facts: dict[str, dict[str, Any]] = {}
        else:
            baseline_facts = _facts(baseline_slices[kind], "baseline", kind, errors)
        if kind not in candidate_slices:
            errors.append(f"missing candidate slice: {kind}")
            candidate_facts: dict[str, dict[str, Any]] = {}
        else:
            candidate_facts = _facts(candidate_slices[kind], "candidate", kind, errors)
        missing = tuple(sorted(set(baseline_facts) - set(candidate_facts)))
        contradicted: list[str] = []
        unsupported = [
            fact_id
            for fact_id, fact in sorted(candidate_facts.items())
            if not _evidence_refs(fact)
        ]
        for fact_id in sorted(set(baseline_facts) & set(candidate_facts)):
            before = baseline_facts[fact_id]
            after = candidate_facts[fact_id]
            if before.get("value") != after.get("value"):
                contradicted.append(fact_id)
        slices.append(VerticalSliceResult(
            kind=kind,
            factual_non_inferiority=not (missing or contradicted or unsupported),
            missing_fact_ids=missing,
            contradicted_fact_ids=tuple(contradicted),
            unsupported_fact_ids=tuple(unsupported),
        ))

    baseline_metrics = _metrics(baseline, "baseline", errors)
    candidate_metrics = _metrics(candidate, "candidate", errors)
    cost = _metric_non_inferiority(
        baseline_metrics, candidate_metrics, "cost_usd", errors,
    )
    review = _metric_non_inferiority(
        baseline_metrics, candidate_metrics, "review_seconds", errors,
    )
    external = _object(payload.get("external_evidence"), "external_evidence", errors)
    missing_external = tuple(
        name
        for name in _REQUIRED_EXTERNAL_EVIDENCE
        if not _verified_external_entry(external.get(name))
    )
    checks_pass = all(slice_.factual_non_inferiority for slice_ in slices) and cost and review
    qualified = checks_pass and not errors and not missing_external
    return VerticalArtifactReport(
        schema_version="1",
        slices=tuple(slices),
        cost_non_inferiority=cost,
        review_time_non_inferiority=review,
        factual_and_operational_checks_pass=checks_pass,
        missing_external_evidence=missing_external,
        schema_errors=tuple(errors),
        qualification_status=(
            QualificationStatus.QUALIFIED if qualified else QualificationStatus.NOT_QUALIFIED
        ),
    )


def vertical_artifact_report_to_json(report: VerticalArtifactReport) -> dict[str, Any]:
    payload = asdict(report)
    payload["qualification_status"] = report.qualification_status.value
    return payload


def _object(value: object, label: str, errors: list[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        errors.append(f"{label} must be an object")
        return {}
    return value


def _slice_index(
    artifact: dict[str, Any], side: str, errors: list[str],
) -> dict[str, dict[str, Any]]:
    raw = artifact.get("slices")
    if not isinstance(raw, list):
        errors.append(f"{side}.slices must be a list")
        return {}
    result: dict[str, dict[str, Any]] = {}
    for entry in raw:
        if not isinstance(entry, dict) or not isinstance(entry.get("kind"), str):
            errors.append(f"{side}.slices entries must identify a kind")
            continue
        kind = entry["kind"]
        if kind in result:
            errors.append(f"{side}.slices duplicate kind: {kind}")
        result[kind] = entry
    return result


def _facts(
    slice_: dict[str, Any], side: str, kind: str, errors: list[str],
) -> dict[str, dict[str, Any]]:
    raw = slice_.get("facts")
    if not isinstance(raw, list):
        errors.append(f"{side}.{kind}.facts must be a list")
        return {}
    result: dict[str, dict[str, Any]] = {}
    for fact in raw:
        if not isinstance(fact, dict) or not isinstance(fact.get("fact_id"), str):
            errors.append(f"{side}.{kind}.facts entries must identify fact_id")
            continue
        fact_id = fact["fact_id"]
        if fact_id in result:
            errors.append(f"{side}.{kind}.facts duplicate fact_id: {fact_id}")
        result[fact_id] = fact
    return result


def _metrics(
    artifact: dict[str, Any], side: str, errors: list[str],
) -> dict[str, dict[str, Any]]:
    raw = artifact.get("metrics")
    if not isinstance(raw, dict):
        errors.append(f"{side}.metrics must be an object")
        return {}
    return {
        key: value for key, value in raw.items()
        if isinstance(key, str) and isinstance(value, dict)
    }


def _metric_non_inferiority(
    baseline: dict[str, dict[str, Any]],
    candidate: dict[str, dict[str, Any]],
    metric: str,
    errors: list[str],
) -> bool:
    before = baseline.get(metric)
    after = candidate.get(metric)
    if before is None or after is None:
        errors.append(f"missing required metric: {metric}")
        return False
    before_value = before.get("value")
    after_value = after.get("value")
    if (
        not isinstance(before_value, (int, float))
        or isinstance(before_value, bool)
        or not math.isfinite(before_value)
        or not isinstance(after_value, (int, float))
        or isinstance(after_value, bool)
        or not math.isfinite(after_value)
    ):
        errors.append(f"{metric} values must be finite numbers")
        return False
    if not _evidence_refs(before):
        errors.append(f"missing baseline metric evidence: {metric}")
    if not _evidence_refs(after):
        errors.append(f"missing candidate metric evidence: {metric}")
    return after_value <= before_value


def _evidence_refs(value: dict[str, Any]) -> tuple[str, ...]:
    refs = value.get("evidence_refs")
    return tuple(ref for ref in refs if isinstance(ref, str) and ref) if isinstance(refs, list) else ()


def _verified_external_entry(value: object) -> bool:
    if not isinstance(value, dict) or value.get("status") != "verified":
        return False
    return bool(_evidence_refs(value))


__all__ = [
    "QualificationStatus",
    "VerticalArtifactReport",
    "VerticalSliceResult",
    "evaluate_vertical_artifact_protocol",
    "vertical_artifact_report_to_json",
]
