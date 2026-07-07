from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import uuid4

from src.core.calibration_engine import CalibrationRollup
from src.core.feedback._advisory_yaml import load_advisory_yaml, write_advisory_yaml
from src.core.models import Confidence
from src.core.models_v2 import ForecastCalibrationModifier, WorkstreamCalibration


REPO_ROOT = Path(__file__).resolve().parents[3]
PROGRAMS_ROOT = REPO_ROOT / "programs"
SCHEMA_VERSION = "1.0"


@dataclass(frozen=True, slots=True)
class ForecastCalibrationDriProfile:
    dri_alias: str
    claim_accuracy: float | None
    sample_size: int
    met: int
    contradicted: int
    stale: int
    slip_modifier: float


def get_forecast_calibration_path(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return programs_root / program_id / "_feedback" / "forecast_calibration.yaml"


def build_forecast_calibration_modifier(
    *,
    workstream_rows: tuple[WorkstreamCalibration, ...],
    dri_rows: tuple[CalibrationRollup, ...],
) -> ForecastCalibrationModifier:
    workstream_modifiers = {
        row.workstream_id: _slip_modifier(row.claim_accuracy)
        for row in workstream_rows
        if row.claim_accuracy is not None
    }
    dri_modifiers = {
        row.subject_id: _slip_modifier(row.claim_accuracy)
        for row in dri_rows
        if row.claim_accuracy is not None
    }
    return ForecastCalibrationModifier(
        workstream_modifiers=workstream_modifiers,
        dri_modifiers=dri_modifiers,
        confidence=_modifier_confidence(
            workstream_rows=workstream_rows,
            dri_rows=dri_rows,
        ),
    )


def refresh_forecast_calibration(
    program_id: str,
    *,
    workstream_rows: tuple[WorkstreamCalibration, ...],
    dri_rows: tuple[CalibrationRollup, ...],
    as_of: datetime,
    since: date | None = None,
    programs_root: Path = PROGRAMS_ROOT,
    dry_run: bool = False,
) -> tuple[ForecastCalibrationModifier, Path | None]:
    modifier = build_forecast_calibration_modifier(
        workstream_rows=workstream_rows,
        dri_rows=dri_rows,
    )
    if dry_run:
        return modifier, None
    path = write_forecast_calibration(
        program_id,
        modifier=modifier,
        workstream_rows=workstream_rows,
        dri_rows=dri_rows,
        as_of=as_of,
        since=since,
        programs_root=programs_root,
    )
    return modifier, path


def load_forecast_calibration_modifier(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> ForecastCalibrationModifier | None:
    payload = load_advisory_yaml(get_forecast_calibration_path(program_id, programs_root=programs_root))
    if payload is None:
        return None
    workstream_modifiers = _coerce_modifier_map(payload.get("workstream_modifiers"))
    dri_modifiers = _coerce_modifier_map(payload.get("dri_modifiers"))
    confidence_value = str(payload.get("confidence") or Confidence.NONE.value)
    return ForecastCalibrationModifier(
        workstream_modifiers=workstream_modifiers,
        dri_modifiers=dri_modifiers,
        confidence=Confidence.from_string(confidence_value),
    )


def load_forecast_calibration_dri_profile(
    program_id: str,
    dri_alias: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> ForecastCalibrationDriProfile | None:
    payload = load_advisory_yaml(get_forecast_calibration_path(program_id, programs_root=programs_root))
    if payload is None:
        return None
    dris = payload.get("dris")
    if not isinstance(dris, dict):
        return None
    normalized_alias = dri_alias.strip().lower()
    raw_profile = dris.get(normalized_alias)
    if not isinstance(raw_profile, dict):
        return None
    return ForecastCalibrationDriProfile(
        dri_alias=normalized_alias,
        claim_accuracy=_coerce_optional_float(raw_profile.get("claim_accuracy")),
        sample_size=_coerce_int(raw_profile.get("sample_size")),
        met=_coerce_int(raw_profile.get("met")),
        contradicted=_coerce_int(raw_profile.get("contradicted")),
        stale=_coerce_int(raw_profile.get("stale")),
        slip_modifier=round(_coerce_optional_float(raw_profile.get("slip_modifier")) or 0.0, 2),
    )


def write_forecast_calibration(
    program_id: str,
    *,
    modifier: ForecastCalibrationModifier,
    workstream_rows: tuple[WorkstreamCalibration, ...],
    dri_rows: tuple[CalibrationRollup, ...],
    as_of: datetime,
    since: date | None = None,
    programs_root: Path = PROGRAMS_ROOT,
) -> Path:
    timestamp = _ensure_utc(as_of)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": timestamp.isoformat(),
        "since": None if since is None else since.isoformat(),
        "confidence": modifier.confidence.value,
        "workstream_modifiers": modifier.workstream_modifiers,
        "dri_modifiers": modifier.dri_modifiers,
        "workstreams": {
            row.workstream_id: {
                "claim_accuracy": row.claim_accuracy,
                "sample_size": row.sample_size,
                "met": row.met,
                "contradicted": row.contradicted,
                "stale": row.stale,
                "slip_modifier": modifier.workstream_modifiers.get(row.workstream_id, 0.0),
            }
            for row in workstream_rows
        },
        "dris": {
            row.subject_id: {
                "claim_accuracy": row.claim_accuracy,
                "sample_size": row.sample_size,
                "met": row.met,
                "contradicted": row.contradicted,
                "stale": row.stale,
                "slip_modifier": modifier.dri_modifiers.get(row.subject_id, 0.0),
            }
            for row in dri_rows
        },
    }
    evidence_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return write_advisory_yaml(
        get_forecast_calibration_path(program_id, programs_root=programs_root),
        payload,
        module_name="calibration_router",
        evidence_hash=evidence_hash,
        generation_run_id=str(uuid4()),
        timestamp=timestamp,
    )


def _coerce_modifier_map(value: object) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, float] = {}
    for key, raw_modifier in value.items():
        try:
            result[str(key)] = round(float(raw_modifier), 2)
        except (TypeError, ValueError):
            continue
    return result


def _coerce_int(value: object) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, (str, float)):
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0
    return 0


def _coerce_optional_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float, str)):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    return None


def _modifier_confidence(
    *,
    workstream_rows: tuple[WorkstreamCalibration, ...],
    dri_rows: tuple[CalibrationRollup, ...],
) -> Confidence:
    qualified_samples = sum(row.sample_size for row in workstream_rows if row.claim_accuracy is not None)
    qualified_samples += sum(row.sample_size for row in dri_rows if row.claim_accuracy is not None)
    if qualified_samples >= 20:
        return Confidence.HIGH
    if qualified_samples >= 10:
        return Confidence.MEDIUM
    if qualified_samples > 0:
        return Confidence.LOW
    return Confidence.NONE


def _slip_modifier(claim_accuracy: float | None) -> float:
    if claim_accuracy is None:
        return 0.0
    return round((1.0 - claim_accuracy) * 0.25, 2)


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)