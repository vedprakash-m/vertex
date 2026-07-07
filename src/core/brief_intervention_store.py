from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import json
from src.core.jsonl_utils import parse_jsonl_line
import os
from pathlib import Path
from typing import Any

import portalocker

from src.core.journal import PROGRAMS_ROOT


SCHEMA_VERSION = "1.0"
INTERVENTIONS_FILENAME = "brief_interventions.jsonl"


class BriefInterventionStatus(str, Enum):
    APPROVED = "approved"
    DISMISSED = "dismissed"


@dataclass(frozen=True, slots=True)
class BriefInterventionResolution:
    proposal_id: str
    status: BriefInterventionStatus
    title: str
    command: str
    source_hash: str
    resolved_at: datetime


def get_brief_interventions_path(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> Path:
    return programs_root / program_id / "_feedback" / INTERVENTIONS_FILENAME


def load_brief_intervention_resolutions(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> dict[str, BriefInterventionResolution]:
    path = get_brief_interventions_path(program_id, programs_root=programs_root)
    if not path.exists():
        return {}

    latest_by_id: dict[str, BriefInterventionResolution] = {}
    with path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            payload = parse_jsonl_line(line)
            if not isinstance(payload, dict):
                raise ValueError(f"Expected JSON object in {path}.")
            resolution = _resolution_from_record(payload)
            latest_by_id[resolution.proposal_id] = resolution
    return latest_by_id


def append_brief_intervention_resolution(
    program_id: str,
    *,
    proposal_id: str,
    title: str,
    command: str,
    source_hash: str,
    status: BriefInterventionStatus,
    resolved_at: datetime | None = None,
    programs_root: Path = PROGRAMS_ROOT,
) -> Path:
    record = {
        "schema_version": SCHEMA_VERSION,
        "proposal_id": proposal_id.strip(),
        "status": status.value,
        "title": title.strip(),
        "command": command.strip(),
        "source_hash": source_hash.strip(),
        "resolved_at": _ensure_utc(resolved_at or datetime.now(timezone.utc)).isoformat(),
    }
    path = get_brief_interventions_path(program_id, programs_root=programs_root)
    _append_jsonl(path, record)
    return path


def _resolution_from_record(record: dict[str, Any]) -> BriefInterventionResolution:
    return BriefInterventionResolution(
        proposal_id=_required_string(record.get("proposal_id"), field_name="proposal_id").strip(),
        status=BriefInterventionStatus(_required_string(record.get("status"), field_name="status").strip()),
        title=_required_string(record.get("title"), field_name="title").strip(),
        command=_required_string(record.get("command"), field_name="command").strip(),
        source_hash=_required_string(record.get("source_hash"), field_name="source_hash").strip(),
        resolved_at=_parse_datetime(record.get("resolved_at"), field_name="resolved_at"),
    )


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(record, sort_keys=True) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        portalocker.lock(handle, portalocker.LOCK_EX)
        try:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            portalocker.unlock(handle)


def _parse_datetime(value: Any, *, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"missing {field_name}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"invalid {field_name}") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include timezone information")
    return parsed.astimezone(timezone.utc)


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _required_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    return value