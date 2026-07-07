from __future__ import annotations

from datetime import date, datetime
import os
from pathlib import Path
import shutil
from typing import Any

import yaml

from src.core.exceptions import ConfigError
from src.core.journal import PROGRAMS_ROOT
from src.core.models_v2 import Assumption, AssumptionStatus


def get_assumptions_path(program_id: str, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return programs_root / program_id / "assumptions.yaml"


def load_assumptions(program_id: str, programs_root: Path = PROGRAMS_ROOT) -> tuple[Assumption, ...]:
    path = get_assumptions_path(program_id, programs_root)
    if not path.exists():
        return ()

    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as error:
        raise ConfigError(f"Invalid YAML in {path}.") from error

    if not isinstance(document, dict):
        raise ConfigError(f"Expected mapping in {path}.")

    schema_version = document.get("schema_version")
    if not isinstance(schema_version, str) or schema_version.split(".", 1)[0] != "1":
        raise ConfigError(f"Unsupported assumptions schema_version {schema_version!r} in {path}.")

    raw_entries = document.get("assumptions")
    if raw_entries is None:
        raw_entries = []
    if not isinstance(raw_entries, list):
        raise ConfigError(f"Expected 'assumptions' list in {path}.")

    entries: list[Assumption] = []
    seen_ids: set[str] = set()
    for index, raw_entry in enumerate(raw_entries, start=1):
        if not isinstance(raw_entry, dict):
            raise ConfigError(f"Assumption entry #{index} in {path} must be a mapping.")
        try:
            entry = _parse_assumption(program_id, raw_entry)
        except (KeyError, TypeError, ValueError) as error:
            raise ConfigError(f"Invalid assumption entry #{index} in {path}: {error}") from error
        if entry.id in seen_ids:
            raise ConfigError(f"Duplicate assumption id '{entry.id}' in {path}.")
        seen_ids.add(entry.id)
        entries.append(entry)
    return tuple(entries)


def save_assumptions(program_id: str, entries: tuple[Assumption, ...], programs_root: Path = PROGRAMS_ROOT) -> None:
    path = get_assumptions_path(program_id, programs_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_suffix(f"{path.suffix}.bak"))

    document = {
        "schema_version": "1.0",
        "assumptions": [_assumption_to_record(entry) for entry in entries],
    }
    temp_path = path.with_suffix(f"{path.suffix}.tmp")
    temp_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    os.replace(temp_path, path)


def check_validation_due(assumptions: tuple[Assumption, ...], as_of: date) -> tuple[Assumption, ...]:
    return tuple(
        assumption
        for assumption in assumptions
        if assumption.status is AssumptionStatus.UNVALIDATED
        and assumption.validation_due is not None
        and assumption.validation_due < as_of
    )


def _parse_assumption(program_id: str, raw_entry: dict[str, Any]) -> Assumption:
    assumption_id = _required_string(raw_entry.get("id"), field_name="id").strip()
    text = _required_string(raw_entry.get("text"), field_name="text").strip()
    raw_program_id = _optional_string(raw_entry.get("program_id"), field_name="program_id") or program_id
    if not assumption_id:
        raise ValueError("missing id")
    if not text:
        raise ValueError("missing text")

    return Assumption(
        id=assumption_id,
        program_id=raw_program_id,
        text=text,
        validation_method=_optional_string(raw_entry.get("validation_method"), field_name="validation_method"),
        validation_due=_parse_optional_date(raw_entry.get("validation_due"), field_name="validation_due"),
        status=AssumptionStatus.from_string(_required_string(raw_entry.get("status"), field_name="status")),
        category=_optional_string(raw_entry.get("category"), field_name="category"),
        linked_risk_id=_optional_string(raw_entry.get("linked_risk_id"), field_name="linked_risk_id"),
        linked_workstream_ids=_parse_string_tuple(raw_entry.get("linked_workstream_ids"), field_name="linked_workstream_ids"),
        linked_milestone_id=_optional_string(raw_entry.get("linked_milestone_id"), field_name="linked_milestone_id"),
        owner_alias=_optional_string(raw_entry.get("owner_alias"), field_name="owner_alias"),
        identified_date=_parse_required_date(raw_entry.get("identified_date"), field_name="identified_date"),
        entity_refs=_parse_string_tuple(raw_entry.get("entity_refs"), field_name="entity_refs"),
        resolved_date=_parse_optional_date(raw_entry.get("resolved_date"), field_name="resolved_date"),
        linked_milestone_ids=_parse_string_tuple(raw_entry.get("linked_milestone_ids"), field_name="linked_milestone_ids"),
        last_reviewed_date=_parse_optional_date(raw_entry.get("last_reviewed_date"), field_name="last_reviewed_date"),
    )


def _assumption_to_record(entry: Assumption) -> dict[str, Any]:
    return {
        "id": entry.id,
        "program_id": entry.program_id,
        "text": entry.text,
        "validation_method": entry.validation_method,
        "validation_due": entry.validation_due.isoformat() if entry.validation_due is not None else None,
        "status": entry.status.value,
        "category": entry.category,
        "linked_risk_id": entry.linked_risk_id,
        "linked_workstream_ids": list(entry.linked_workstream_ids),
        "linked_milestone_id": entry.linked_milestone_id,
        "owner_alias": entry.owner_alias,
        "identified_date": entry.identified_date.isoformat(),
        "entity_refs": list(entry.entity_refs),
        "resolved_date": entry.resolved_date.isoformat() if entry.resolved_date is not None else None,
        "linked_milestone_ids": list(entry.linked_milestone_ids),
        "last_reviewed_date": entry.last_reviewed_date.isoformat() if entry.last_reviewed_date is not None else None,
    }


def _parse_required_date(value: object, *, field_name: str) -> date:
    parsed = _parse_optional_date(value, field_name=field_name)
    if parsed is None:
        raise ValueError(f"missing {field_name}")
    return parsed


def _parse_optional_date(value: object, *, field_name: str) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if not isinstance(value, str):
        raise ValueError(f"invalid {field_name}")
    return date.fromisoformat(value)


def _required_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    return value


def _parse_string_tuple(value: object, *, field_name: str) -> tuple[str, ...]:
    raw_values = value or ()
    if not isinstance(raw_values, (list, tuple)):
        raise ValueError(f"{field_name} must be a list of strings")
    parsed: list[str] = []
    for entry in raw_values:
        if not isinstance(entry, str):
            raise TypeError(f"{field_name} must contain strings only")
        text = entry.strip()
        if text:
            parsed.append(text)
    return tuple(parsed)


def _optional_string(value: object, *, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    text = value.strip()
    return text or None