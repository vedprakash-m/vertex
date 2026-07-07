from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from src.core.jsonl_utils import parse_jsonl_line
import os
from pathlib import Path

import portalocker

from src.core.edition_resolver import PROGRAMS_ROOT


FILENAME = "claim_extraction_calibration.jsonl"


@dataclass(frozen=True, slots=True)
class ClaimExtractionCalibrationRecord:
    program_id: str
    issue_number: int
    recorded_at: datetime
    mode: str
    ai_claim_count: int
    regex_claim_count: int
    shared_claim_count: int
    ai_only_count: int
    regex_only_count: int
    agreement_rate: float


@dataclass(frozen=True, slots=True)
class ClaimExtractionCalibrationSummary:
    calibration_sample_count: int
    recent_sample_count: int
    recent_agreement_rate: float
    recent_average_difference_count: float
    last_recorded_at: datetime | None


def get_claim_extraction_calibration_path(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> Path:
    return programs_root / program_id / "_feedback" / FILENAME


def append_claim_extraction_calibration_record(
    record: ClaimExtractionCalibrationRecord,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> Path:
    path = get_claim_extraction_calibration_path(record.program_id, programs_root=programs_root)
    _append_jsonl(path, _record_to_payload(record))
    return path


def load_claim_extraction_calibration_records(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[ClaimExtractionCalibrationRecord, ...]:
    path = get_claim_extraction_calibration_path(program_id, programs_root=programs_root)
    if not path.exists():
        return ()

    rows: list[ClaimExtractionCalibrationRecord] = []
    with path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            payload = parse_jsonl_line(line)
            if not isinstance(payload, dict):
                raise ValueError(f"Expected JSON object in {path}, found {type(payload).__name__}.")
            rows.append(_record_from_payload(program_id, payload))
    rows.sort(key=lambda row: (row.recorded_at, row.issue_number))
    return tuple(rows)


def summarize_claim_extraction_calibration(
    program_id: str,
    *,
    recent_cycles: int = 10,
    programs_root: Path = PROGRAMS_ROOT,
) -> ClaimExtractionCalibrationSummary:
    records = [
        record
        for record in load_claim_extraction_calibration_records(program_id, programs_root=programs_root)
        if record.mode == "calibration"
    ]
    if recent_cycles <= 0:
        recent_records: list[ClaimExtractionCalibrationRecord] = []
    else:
        recent_records = records[-recent_cycles:]
    recent_agreement_rate = (
        round(sum(record.agreement_rate for record in recent_records) / len(recent_records), 4)
        if recent_records
        else 0.0
    )
    recent_average_difference_count = (
        round(
            sum(record.ai_only_count + record.regex_only_count for record in recent_records) / len(recent_records),
            4,
        )
        if recent_records
        else 0.0
    )
    return ClaimExtractionCalibrationSummary(
        calibration_sample_count=len(records),
        recent_sample_count=len(recent_records),
        recent_agreement_rate=recent_agreement_rate,
        recent_average_difference_count=recent_average_difference_count,
        last_recorded_at=(records[-1].recorded_at if records else None),
    )


def _record_to_payload(record: ClaimExtractionCalibrationRecord) -> dict[str, object]:
    return {
        "issue_number": record.issue_number,
        "recorded_at": _ensure_utc(record.recorded_at).isoformat(),
        "mode": record.mode,
        "ai_claim_count": record.ai_claim_count,
        "regex_claim_count": record.regex_claim_count,
        "shared_claim_count": record.shared_claim_count,
        "ai_only_count": record.ai_only_count,
        "regex_only_count": record.regex_only_count,
        "agreement_rate": record.agreement_rate,
    }


def _record_from_payload(
    program_id: str,
    payload: dict[str, object],
) -> ClaimExtractionCalibrationRecord:
    return ClaimExtractionCalibrationRecord(
        program_id=program_id,
        issue_number=_required_int(payload["issue_number"], field_name="issue_number"),
        recorded_at=_parse_required_datetime(payload.get("recorded_at"), field_name="recorded_at"),
        mode=_required_string(payload.get("mode"), field_name="mode"),
        ai_claim_count=_required_int(payload.get("ai_claim_count"), field_name="ai_claim_count"),
        regex_claim_count=_required_int(payload.get("regex_claim_count"), field_name="regex_claim_count"),
        shared_claim_count=_required_int(payload.get("shared_claim_count"), field_name="shared_claim_count"),
        ai_only_count=_required_int(payload.get("ai_only_count"), field_name="ai_only_count"),
        regex_only_count=_required_int(payload.get("regex_only_count"), field_name="regex_only_count"),
        agreement_rate=_required_float(payload.get("agreement_rate"), field_name="agreement_rate"),
    )


def _required_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer")
    return value


def _required_float(value: object, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field_name} must be numeric")
    return float(value)


def _required_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    return value


def _append_jsonl(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, ensure_ascii=False) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        portalocker.lock(handle, portalocker.LOCK_EX)
        try:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            portalocker.unlock(handle)


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_required_datetime(value: object, *, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include timezone information")
    return _ensure_utc(parsed)