from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, timezone
from enum import Enum
import json
import os
from pathlib import Path
from typing import Any

import portalocker

from src.core.journal import PROGRAMS_ROOT
from src.core.jsonl_utils import (
    compute_file_checksum,
    jsonl_checksum_matches,
    list_jsonl_quarantine_paths,
    parse_jsonl_line,
    quarantine_and_rewrite_jsonl,
    write_checksum_file,
)
from src.core.models import RiskLevel
from src.core.models_v2 import TrajectoryPoint


def get_program_trajectory_dir(program_id: str, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return programs_root / program_id / "trajectories"


def get_trajectory_path(program_id: str, work_item_id: int, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return get_program_trajectory_dir(program_id, programs_root) / f"{work_item_id}.jsonl"


def get_trajectory_quarantine_dir(program_id: str, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return get_program_trajectory_dir(program_id, programs_root) / "quarantine"


def get_trajectory_checksum_path(program_id: str, work_item_id: int, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return get_trajectory_path(program_id, work_item_id, programs_root).with_suffix(".sha256")


def list_trajectory_quarantine_paths(program_id: str, programs_root: Path = PROGRAMS_ROOT) -> tuple[Path, ...]:
    quarantine_dir = get_trajectory_quarantine_dir(program_id, programs_root)
    return list_jsonl_quarantine_paths(quarantine_dir)


def trajectory_checksum_matches(
    program_id: str,
    work_item_id: int,
    programs_root: Path = PROGRAMS_ROOT,
) -> bool | None:
    path = get_trajectory_path(program_id, work_item_id, programs_root)
    checksum_path = get_trajectory_checksum_path(program_id, work_item_id, programs_root)
    return jsonl_checksum_matches(path, checksum_path)


def append_trajectory_point(
    program_id: str,
    work_item_id: int,
    point: TrajectoryPoint,
    programs_root: Path = PROGRAMS_ROOT,
) -> bool:
    existing = read_trajectory(program_id, work_item_id, programs_root=programs_root)
    if existing and not _has_material_change(existing[-1], point):
        return False
    target = get_trajectory_path(program_id, work_item_id, programs_root)
    _append_jsonl(target, _trajectory_point_to_record(point))
    return True


def backfill_trajectory_points(
    program_id: str,
    work_item_id: int,
    points: tuple[TrajectoryPoint, ...],
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> int:
    path = get_trajectory_path(program_id, work_item_id, programs_root)
    seen = {_trajectory_point_identity(point) for point in read_trajectory(program_id, work_item_id, programs_root=programs_root)}
    appended = 0
    for point in sorted(points, key=lambda entry: entry.date):
        identity = _trajectory_point_identity(point)
        if identity in seen:
            continue
        _append_jsonl(path, _trajectory_point_to_record(point))
        seen.add(identity)
        appended += 1
    return appended


def read_trajectory(
    program_id: str,
    work_item_id: int,
    *,
    start: date | None = None,
    end: date | None = None,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[TrajectoryPoint, ...]:
    path = get_trajectory_path(program_id, work_item_id, programs_root)
    if not path.exists():
        return ()
    points: list[TrajectoryPoint] = []
    for record in _read_jsonl(path):
        point = _trajectory_point_from_record(record)
        if start is not None and point.date < start:
            continue
        if end is not None and point.date > end:
            continue
        points.append(point)
    points.sort(key=lambda entry: entry.date)
    return tuple(points)


def load_latest_trajectory_point(
    program_id: str,
    work_item_id: int,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> TrajectoryPoint | None:
    points = read_trajectory(program_id, work_item_id, programs_root=programs_root)
    return points[-1] if points else None


def load_all_trajectories(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> dict[int, tuple[TrajectoryPoint, ...]]:
    """FR-SG-21: Return all trajectories for a program keyed by work item id."""
    traj_dir = get_program_trajectory_dir(program_id, programs_root)
    if not traj_dir.exists():
        return {}
    result: dict[int, tuple[TrajectoryPoint, ...]] = {}
    for jsonl_path in sorted(traj_dir.glob("*.jsonl")):
        try:
            work_item_id = int(jsonl_path.stem)
        except ValueError:
            continue
        points = read_trajectory(program_id, work_item_id, programs_root=programs_root)
        if points:
            result[work_item_id] = points
    return result


def _has_material_change(previous: TrajectoryPoint, current: TrajectoryPoint) -> bool:
    return (
        previous.state != current.state
        or previous.assigned_to != current.assigned_to
        or previous.target_date != current.target_date
        or previous.risk_level != current.risk_level
        or previous.risk_assessment != current.risk_assessment
        or previous.risk_assessment_comment != current.risk_assessment_comment
        or previous.area_path != current.area_path
        or previous.tags != current.tags
    )


def _trajectory_point_to_record(point: TrajectoryPoint) -> dict[str, Any]:
    return {
        "date": point.date.isoformat(),
        "state": point.state,
        "assigned_to": point.assigned_to,
        "target_date": point.target_date.isoformat() if point.target_date is not None else None,
        "risk_level": point.risk_level.value if point.risk_level is not None else None,
        "risk_assessment": point.risk_assessment,
        "risk_assessment_comment": point.risk_assessment_comment,
        "area_path": point.area_path,
        "tags": list(point.tags),
    }


def _trajectory_point_from_record(record: dict[str, Any]) -> TrajectoryPoint:
    raw_risk_level = record.get("risk_level")
    return TrajectoryPoint(
        date=_parse_date(record["date"]),
        state=str(record["state"]),
        assigned_to=_optional_string(record.get("assigned_to")),
        target_date=_parse_date(record["target_date"]) if record.get("target_date") is not None else None,
        risk_level=RiskLevel.from_string(str(raw_risk_level)) if raw_risk_level is not None else None,
        area_path=str(record["area_path"]),
        tags=tuple(str(tag) for tag in record.get("tags") or ()),
        risk_assessment=_optional_string(record.get("risk_assessment")),
        risk_assessment_comment=_optional_string(record.get("risk_assessment_comment")),
    )


def _trajectory_point_identity(point: TrajectoryPoint) -> tuple[object, ...]:
    return (
        point.date.isoformat(),
        point.state,
        point.assigned_to,
        point.target_date.isoformat() if point.target_date is not None else None,
        point.risk_level.value if point.risk_level is not None else None,
        point.risk_assessment,
        point.risk_assessment_comment,
        point.area_path,
        point.tags,
    )


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(record, ensure_ascii=False, default=_json_default) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        portalocker.lock(handle, portalocker.LOCK_EX)
        try:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            portalocker.unlock(handle)
    write_checksum_file(path)


def _read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    valid_lines: list[str] = []
    invalid_found = False
    with path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = parse_jsonl_line(line)
            except json.JSONDecodeError:
                invalid_found = True
                continue
            if not isinstance(payload, dict):
                invalid_found = True
                continue
            valid_lines.append(raw_line if raw_line.endswith("\n") else raw_line + "\n")
            yield payload

    if invalid_found:
        quarantine_and_rewrite_jsonl(path, valid_lines)


def _parse_date(value: Any) -> date:
    if not isinstance(value, str):
        raise ValueError(f"Expected ISO date string, found {type(value).__name__}.")
    return date.fromisoformat(value)


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _json_default(value: Any) -> Any:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")