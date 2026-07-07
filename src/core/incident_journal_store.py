from __future__ import annotations

from datetime import datetime, timezone
import json
from src.core.jsonl_utils import parse_jsonl_line
import os
from pathlib import Path
from typing import Any

import portalocker

from src.core.models import Confidence
from src.core.models_v2 import IncidentEntry


REPO_ROOT = Path(__file__).resolve().parents[2]
PROGRAMS_ROOT = REPO_ROOT / "programs"


def get_incident_journal_path(program_id: str, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return programs_root / program_id / "journal" / "incident_journal.jsonl"


def append_incident_entry(entry: IncidentEntry, programs_root: Path = PROGRAMS_ROOT) -> Path:
    target = get_incident_journal_path(entry.program_id, programs_root)
    _append_jsonl(target, _incident_entry_to_record(entry))
    return target


def read_incident_entries(
    program_id: str,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[IncidentEntry, ...]:
    path = get_incident_journal_path(program_id, programs_root)
    if not path.exists():
        return ()
    start_ts = _ensure_utc(start) if start is not None else None
    end_ts = _ensure_utc(end) if end is not None else None
    entries: list[IncidentEntry] = []
    for record in _read_jsonl(path):
        entry = _incident_entry_from_record(record)
        if start_ts is not None and entry.recorded_at < start_ts:
            continue
        if end_ts is not None and entry.recorded_at > end_ts:
            continue
        entries.append(entry)
    entries.sort(key=lambda entry: (entry.recorded_at, entry.signal_id))
    return tuple(entries)


def _incident_entry_to_record(entry: IncidentEntry) -> dict[str, Any]:
    return {
        "schema_version": entry.schema_version,
        "program_id": entry.program_id,
        "incident_id": entry.incident_id,
        "signal_id": entry.signal_id,
        "observed_at": entry.observed_at.isoformat(),
        "recorded_at": entry.recorded_at.isoformat(),
        "belief_change_summary": entry.belief_change_summary,
        "workstream_id": entry.workstream_id,
        "owning_team": entry.owning_team,
        "severity": entry.severity,
        "source_path": entry.source_path,
        "query_id": entry.query_id,
        "linked_work_item_ids": list(entry.linked_work_item_ids),
        "ado_entity_refs": list(entry.ado_entity_refs),
        "raw_ref": entry.raw_ref,
        "confidence": entry.confidence.value,
    }


def _incident_entry_from_record(record: dict[str, Any]) -> IncidentEntry:
    return IncidentEntry(
        schema_version=_required_string(record.get("schema_version") or "1.0", field_name="schema_version"),
        program_id=_required_string(record["program_id"], field_name="program_id"),
        incident_id=_required_string(record["incident_id"], field_name="incident_id"),
        signal_id=_required_string(record["signal_id"], field_name="signal_id"),
        observed_at=_parse_required_datetime(record["observed_at"], field_name="observed_at"),
        recorded_at=_parse_required_datetime(record["recorded_at"], field_name="recorded_at"),
        belief_change_summary=_required_string(record["belief_change_summary"], field_name="belief_change_summary"),
        workstream_id=_parse_optional_string(record.get("workstream_id"), field_name="workstream_id"),
        owning_team=_parse_optional_string(record.get("owning_team"), field_name="owning_team"),
        severity=_parse_optional_int(record.get("severity"), field_name="severity"),
        source_path=_parse_optional_string(record.get("source_path"), field_name="source_path"),
        query_id=_parse_optional_string(record.get("query_id"), field_name="query_id"),
        linked_work_item_ids=_parse_linked_work_item_ids(record.get("linked_work_item_ids")),
        ado_entity_refs=_parse_ado_entity_refs(record.get("ado_entity_refs")),
        raw_ref=_parse_optional_string(record.get("raw_ref"), field_name="raw_ref"),
        confidence=Confidence.from_string(_required_string(record.get("confidence"), field_name="confidence")),
    )


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(record, separators=(",", ":")) + os.linesep
    with path.open("a", encoding="utf-8") as handle:
        portalocker.lock(handle, portalocker.LOCK_EX)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
        portalocker.unlock(handle)


def _read_jsonl(path: Path) -> tuple[dict[str, Any], ...]:
    entries: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parsed = parse_jsonl_line(stripped)
        if not isinstance(parsed, dict):
            raise TypeError("incident journal rows must be JSON objects")
        entries.append(parsed)
    return tuple(entries)


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _required_string(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    return value


def _parse_required_datetime(value: Any, *, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include timezone information")
    return parsed.astimezone(timezone.utc)


def _parse_optional_string(value: Any, *, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    text = value.strip()
    return text or None


def _parse_optional_int(value: Any, *, field_name: str) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer")
    return value


def _parse_linked_work_item_ids(value: Any) -> tuple[int, ...]:
    if value in (None, ""):
        return ()
    if not isinstance(value, list):
        raise TypeError("linked_work_item_ids must be a list of integers")
    linked_ids: list[int] = []
    for entry in value:
        if isinstance(entry, bool) or not isinstance(entry, int):
            raise TypeError("linked_work_item_ids must contain integers only")
        linked_ids.append(entry)
    return tuple(linked_ids)


def _parse_ado_entity_refs(value: Any) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    if not isinstance(value, list):
        raise TypeError("ado_entity_refs must be a list of strings")
    refs: list[str] = []
    for entry in value:
        if not isinstance(entry, str):
            raise TypeError("ado_entity_refs must contain strings only")
        refs.append(entry)
    return tuple(refs)