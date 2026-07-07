"""FR-SG-17: ProgramEvent / Chronicle append-only store."""

from __future__ import annotations

import json
import os
import portalocker
from src.core.jsonl_utils import parse_jsonl_line
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, cast

from src.core.config_loader import PROGRAMS_ROOT


@dataclass(frozen=True, slots=True)
class ProgramEvent:
    event_type: Literal["pause", "resume", "dfd_slip", "pivot", "commitment", "approval", "pm_steering"]
    event_date: datetime
    description: str
    source: str
    actors: tuple[str, ...]
    linked_dimensions: tuple[str, ...]
    event_id: str | None


def _chronicle_path(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return programs_root / program_id / "chronicle.jsonl"


def append_program_event(
    program_id: str,
    event: ProgramEvent,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> None:
    """Append a ProgramEvent to the program's chronicle (append-only)."""
    path = _chronicle_path(program_id, programs_root=programs_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "event_type": event.event_type,
        "event_date": event.event_date.isoformat(),
        "description": event.description,
        "source": event.source,
        "actors": list(event.actors),
        "linked_dimensions": list(event.linked_dimensions),
        "event_id": event.event_id,
    }
    with path.open("a", encoding="utf-8") as fh:
        portalocker.lock(fh, portalocker.LOCK_EX)
        try:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        finally:
            portalocker.unlock(fh)


def load_program_events(
    program_id: str,
    *,
    after: datetime | None = None,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[ProgramEvent, ...]:
    """Load program events, optionally filtering to those after a timestamp."""
    path = _chronicle_path(program_id, programs_root=programs_root)
    if not path.exists():
        return ()
    events: list[ProgramEvent] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        record = parse_jsonl_line(line)
        if not isinstance(record, dict):
            raise ValueError(f"Expected JSON object in {path}, found {type(record).__name__}.")
        event_date = _parse_required_datetime(record.get("event_date"), field_name="event_date")
        if after is not None and event_date <= after:
            continue
        events.append(
            ProgramEvent(
                event_type=cast(Literal["pause", "resume", "dfd_slip", "pivot", "commitment", "approval", "pm_steering"], _required_string(record.get("event_type"), field_name="event_type")),
                event_date=event_date,
                description=_required_string(record.get("description"), field_name="description"),
                source=_required_string(record.get("source"), field_name="source"),
                actors=_string_tuple(record.get("actors"), field_name="actors"),
                linked_dimensions=_string_tuple(record.get("linked_dimensions"), field_name="linked_dimensions"),
                event_id=_optional_string(record.get("event_id"), field_name="event_id"),
            )
        )
    return tuple(events)


def _required_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    return value


def _optional_string(value: object, *, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    return value


def _string_tuple(value: object, *, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise TypeError(f"{field_name} must be a list of strings")
    normalized: list[str] = []
    for entry in value:
        if not isinstance(entry, str):
            raise TypeError(f"{field_name} entries must be strings")
        normalized.append(entry)
    return tuple(normalized)


def _parse_required_datetime(value: object, *, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
