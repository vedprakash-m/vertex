from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.core.exceptions import StateError
from src.core.models_v2 import IntegrationError
from src.core.program_paths import (
    get_gather_state_path,
    resolve_gather_state_path_for_read,
)


_SKIP_VALUE = object()


@dataclass(frozen=True, slots=True)
class GatherState:
    program_id: str
    gathered_at: datetime
    scanned_items: int
    discovered_signals: int
    new_signals: int
    pending_review: int
    trajectory_updates: int
    auto_reviews_written: int
    ado_calls: int
    archived_journal_files: int
    background_proposals: int
    integration_errors: int = 0
    integration_error_details: tuple[IntegrationError, ...] = ()
    query_states: dict[str, dict[str, Any]] = field(default_factory=dict)
    gather_flags: dict[str, bool] = field(default_factory=dict)
    channels: dict[str, dict[str, Any]] = field(default_factory=dict)
    m365_discovery: dict[str, Any] = field(default_factory=dict)
    previous_gathered_at: datetime | None = None
    previous_query_states: dict[str, dict[str, Any]] = field(default_factory=dict)
    previous_channels: dict[str, dict[str, Any]] = field(default_factory=dict)
    previous_m365_discovery: dict[str, Any] = field(default_factory=dict)
    entity_resolution_rates: dict[str, Any] = field(default_factory=dict)  # WI-2.4: per-scope resolution rates


def build_gather_integration_summary(gather_state: GatherState | None) -> str | None:
    if gather_state is None or gather_state.integration_errors <= 0:
        return None
    if gather_state.integration_error_details:
        detail = gather_state.integration_error_details[0]
        action_suffix = f" Next: {detail.operator_action}" if detail.operator_action else ""
        return (
            f"{gather_state.integration_errors} optional integration failure(s); "
            f"{detail.source}/{detail.stage}: {detail.message}.{action_suffix}"
        )
    return f"{gather_state.integration_errors} optional integration failure(s)."


def build_gather_integration_lines(gather_state: GatherState | None) -> tuple[str, ...]:
    if gather_state is None or gather_state.integration_errors <= 0:
        return ()

    failure_label = "optional integration failure" if gather_state.integration_errors == 1 else "optional integration failures"
    summary_line = f"Latest gather recorded {gather_state.integration_errors} {failure_label}."

    if not gather_state.integration_error_details:
        return (summary_line,)

    lines: list[str] = [summary_line]
    for detail in gather_state.integration_error_details:
        line = f"{detail.source}/{detail.stage}: {detail.message}"
        if detail.operator_action:
            line = f"{line} Next: {detail.operator_action}"
        lines.append(line)
    return tuple(lines)


def load_gather_state(program_id: str, *, programs_root: Path) -> GatherState | None:
    path = resolve_gather_state_path_for_read(program_id, programs_root=programs_root)
    if not path.exists():
        return None

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise StateError(f"Invalid gather state JSON in {path}") from error
    if not isinstance(payload, dict):
        raise StateError(f"Invalid gather state payload in {path}")

    gathered_at = _parse_datetime(payload.get("gathered_at"))
    if gathered_at is None:
        raise StateError(f"Gather state {path} is missing a valid gathered_at timestamp")

    payload_program_id = str(payload.get("program_id") or "").strip()
    if payload_program_id and payload_program_id != program_id:
        raise StateError(f"Gather state {path} belongs to {payload_program_id}, not {program_id}")

    return GatherState(
        program_id=program_id,
        gathered_at=gathered_at,
        scanned_items=_parse_int(payload.get("scanned_items")),
        discovered_signals=_parse_int(payload.get("discovered_signals")),
        new_signals=_parse_int(payload.get("new_signals")),
        pending_review=_parse_int(payload.get("pending_review")),
        trajectory_updates=_parse_int(payload.get("trajectory_updates")),
        auto_reviews_written=_parse_int(payload.get("auto_reviews_written")),
        ado_calls=_parse_int(payload.get("ado_calls")),
        archived_journal_files=_parse_int(payload.get("archived_journal_files")),
        background_proposals=_parse_int(payload.get("background_proposals")),
        integration_errors=_parse_int(payload.get("integration_errors")),
        integration_error_details=_parse_integration_error_details(payload.get("integration_error_details")),
        query_states=_parse_nested_state(payload.get("queries")),
        gather_flags=_parse_gather_flags(payload.get("gather_flags")),
        channels=_parse_nested_state(payload.get("channels")),
        m365_discovery=_parse_state_map(payload.get("m365_discovery")),
        previous_gathered_at=_parse_datetime(payload.get("previous_gathered_at")),
        previous_query_states=_parse_nested_state(payload.get("previous_queries")),
        previous_channels=_parse_nested_state(payload.get("previous_channels")),
        previous_m365_discovery=_parse_state_map(payload.get("previous_m365_discovery")),
    )


def load_gather_query_states(program_id: str, *, programs_root: Path) -> dict[str, dict[str, Any]]:
    path = resolve_gather_state_path_for_read(program_id, programs_root=programs_root)
    if not path.exists():
        return {}

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise StateError(f"Invalid gather state JSON in {path}") from error
    if not isinstance(payload, dict):
        raise StateError(f"Invalid gather state payload in {path}")

    return _parse_nested_state(payload.get("queries"))


def write_gather_state(
    program_id: str,
    *,
    gathered_at: datetime,
    scanned_items: int,
    discovered_signals: int,
    new_signals: int,
    pending_review: int,
    trajectory_updates: int,
    auto_reviews_written: int,
    ado_calls: int,
    archived_journal_files: int,
    background_proposals: int,
    integration_errors: int = 0,
    integration_error_details: tuple[IntegrationError, ...] = (),
    gather_flags: dict[str, bool] | None = None,
    channels: dict[str, dict[str, Any]] | None = None,
    m365_discovery: dict[str, Any] | None = None,
    previous_gathered_at: datetime | None = None,
    previous_query_states: dict[str, dict[str, Any]] | None = None,
    previous_channels: dict[str, dict[str, Any]] | None = None,
    previous_m365_discovery: dict[str, Any] | None = None,
    query_states: dict[str, dict[str, Any]] | None = None,
    programs_root: Path,
) -> Path:
    path = get_gather_state_path(program_id, programs_root=programs_root)
    existing_state = load_gather_state(program_id, programs_root=programs_root) if path.exists() else None
    if existing_state is not None:
        if previous_gathered_at is None:
            previous_gathered_at = existing_state.previous_gathered_at or existing_state.gathered_at
        if previous_query_states is None:
            previous_query_states = existing_state.previous_query_states or existing_state.query_states
        if previous_channels is None:
            previous_channels = existing_state.previous_channels or existing_state.channels
        if previous_m365_discovery is None:
            previous_m365_discovery = existing_state.previous_m365_discovery or existing_state.m365_discovery
    payload = {
        "schema_version": "2.0",
        "program_id": program_id,
        "gathered_at": _normalize_datetime(gathered_at).isoformat(),
        "scanned_items": scanned_items,
        "discovered_signals": discovered_signals,
        "new_signals": new_signals,
        "pending_review": pending_review,
        "trajectory_updates": trajectory_updates,
        "auto_reviews_written": auto_reviews_written,
        "ado_calls": ado_calls,
        "archived_journal_files": archived_journal_files,
        "background_proposals": background_proposals,
        "integration_errors": integration_errors,
        "integration_error_details": [
            {
                "source": detail.source,
                "stage": detail.stage,
                "retryable": detail.retryable,
                "message": detail.message,
                "operator_action": detail.operator_action,
            }
            for detail in integration_error_details
        ],
        "gather_flags": _normalize_gather_flags(gather_flags),
        "channels": _normalize_nested_state(channels),
        "m365_discovery": _normalize_state_map(m365_discovery),
        "previous_gathered_at": _normalize_datetime(previous_gathered_at).isoformat() if previous_gathered_at is not None else None,
        "previous_queries": _normalize_query_states(previous_query_states),
        "previous_channels": _normalize_nested_state(previous_channels),
        "previous_m365_discovery": _normalize_state_map(previous_m365_discovery),
        "queries": _normalize_query_states(query_states),
    }
    _write_atomic_json(path, payload)
    return path


def _write_atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)


def _normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return _normalize_datetime(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _parse_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _parse_integration_error_details(value: Any) -> tuple[IntegrationError, ...]:
    if not isinstance(value, list):
        return ()
    details: list[IntegrationError] = []
    for entry in value:
        if not isinstance(entry, dict):
            continue
        source = str(entry.get("source") or "").strip()
        stage = str(entry.get("stage") or "").strip()
        message = str(entry.get("message") or "").strip()
        if not source or not stage or not message:
            continue
        operator_action_raw = entry.get("operator_action")
        operator_action = str(operator_action_raw).strip() if isinstance(operator_action_raw, str) and operator_action_raw.strip() else None
        details.append(
            IntegrationError(
                source=source,
                stage=stage,
                retryable=bool(entry.get("retryable")),
                message=message,
                operator_action=operator_action,
            )
        )
    return tuple(details)


def _parse_gather_flags(value: Any) -> dict[str, bool]:
    if not isinstance(value, dict):
        return {}
    parsed: dict[str, bool] = {}
    for key, raw in value.items():
        normalized_key = str(key).strip()
        if not normalized_key or not isinstance(raw, bool):
            continue
        parsed[normalized_key] = raw
    return parsed


def _parse_state_map(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    parsed: dict[str, Any] = {}
    for key, raw in value.items():
        normalized_key = str(key).strip()
        if not normalized_key:
            continue
        normalized_value = _parse_nested_state_value(raw)
        if normalized_value is not _SKIP_VALUE:
            parsed[normalized_key] = normalized_value
    return parsed


def _parse_nested_state(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        return {}
    parsed: dict[str, dict[str, Any]] = {}
    for outer_key, raw_state in value.items():
        normalized_outer_key = str(outer_key).strip()
        if not normalized_outer_key or not isinstance(raw_state, dict):
            continue
        normalized_state: dict[str, Any] = {}
        for key, raw in raw_state.items():
            field_key = str(key).strip()
            if not field_key:
                continue
            normalized_value = _parse_nested_state_value(raw)
            if normalized_value is not _SKIP_VALUE:
                normalized_state[field_key] = normalized_value
        parsed[normalized_outer_key] = normalized_state
    return parsed


def _normalize_gather_flags(value: dict[str, bool] | None) -> dict[str, bool]:
    if not isinstance(value, dict):
        return {}
    normalized: dict[str, bool] = {}
    for key, raw in value.items():
        normalized_key = str(key).strip()
        if not normalized_key or not isinstance(raw, bool):
            continue
        normalized[normalized_key] = raw
    return normalized


def _normalize_state_map(value: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    normalized: dict[str, Any] = {}
    for key, raw in value.items():
        normalized_key = str(key).strip()
        if not normalized_key:
            continue
        if isinstance(raw, datetime):
            normalized[normalized_key] = _normalize_datetime(raw).isoformat().replace("+00:00", "Z")
            continue
        if isinstance(raw, (str, int, float, bool)) or raw is None:
            normalized[normalized_key] = raw
            continue
        if isinstance(raw, list):
            normalized_list = _normalize_nested_state_list(raw)
            if normalized_list is not _SKIP_VALUE:
                normalized[normalized_key] = normalized_list
    return normalized


def _normalize_nested_state(value: dict[str, dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        return {}
    normalized: dict[str, dict[str, Any]] = {}
    for outer_key, raw_state in value.items():
        normalized_outer_key = str(outer_key).strip()
        if not normalized_outer_key or not isinstance(raw_state, dict):
            continue
        normalized_state: dict[str, Any] = {}
        for key, raw in raw_state.items():
            field_key = str(key).strip()
            if not field_key:
                continue
            if isinstance(raw, (str, int, float, bool)) or raw is None:
                normalized_state[field_key] = raw
                continue
            if isinstance(raw, datetime):
                normalized_state[field_key] = _normalize_datetime(raw).isoformat().replace("+00:00", "Z")
                continue
            if isinstance(raw, list):
                normalized_list = _normalize_nested_state_list(raw)
                if normalized_list is not _SKIP_VALUE:
                    normalized_state[field_key] = normalized_list
                continue
            if isinstance(raw, dict):
                normalized_map = _normalize_nested_state_map(raw)
                if normalized_map is not _SKIP_VALUE:
                    normalized_state[field_key] = normalized_map
        normalized[normalized_outer_key] = normalized_state
    return normalized


def _normalize_query_states(value: dict[str, dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        return {}

    normalized: dict[str, dict[str, Any]] = {}
    for query_id, state in value.items():
        query_key = str(query_id).strip()
        if not query_key or not isinstance(state, dict):
            continue
        normalized_state: dict[str, Any] = {}
        for key, raw in state.items():
            field_key = str(key).strip()
            if not field_key:
                continue
            if isinstance(raw, datetime):
                normalized_state[field_key] = _normalize_datetime(raw).isoformat().replace("+00:00", "Z")
                continue
            if isinstance(raw, (str, int, float, bool)) or raw is None:
                normalized_state[field_key] = raw
                continue
            if isinstance(raw, list):
                normalized_list = _normalize_nested_state_list(raw)
                if normalized_list is not _SKIP_VALUE:
                    normalized_state[field_key] = normalized_list
        normalized[query_key] = normalized_state
    return normalized


def _parse_nested_state_value(raw: Any) -> Any:
    if isinstance(raw, (str, int, float, bool)) or raw is None:
        return raw
    if isinstance(raw, list):
        values: list[Any] = []
        for item in raw:
            if isinstance(item, (str, int, float, bool)) or item is None:
                values.append(item)
                continue
            if isinstance(item, dict):
                normalized_map = _parse_nested_state_map(item)
                if normalized_map is _SKIP_VALUE:
                    return _SKIP_VALUE
                values.append(normalized_map)
        return values
    if isinstance(raw, dict):
        return _parse_nested_state_map(raw)
    return _SKIP_VALUE


def _normalize_nested_state_list(raw: list[Any]) -> list[Any] | object:
    values: list[Any] = []
    for item in raw:
        if isinstance(item, (str, int, float, bool)) or item is None:
            values.append(item)
            continue
        if isinstance(item, dict):
            normalized_map = _normalize_nested_state_map(item)
            if normalized_map is _SKIP_VALUE:
                return _SKIP_VALUE
            values.append(normalized_map)
            continue
        return _SKIP_VALUE
    return values


def _parse_nested_state_map(raw: dict[Any, Any]) -> dict[str, Any] | object:
    values: dict[str, Any] = {}
    for key, item in raw.items():
        normalized_key = str(key).strip()
        if not normalized_key:
            continue
        normalized_value = _parse_nested_state_value(item)
        if normalized_value is _SKIP_VALUE:
            return _SKIP_VALUE
        values[normalized_key] = normalized_value
    return values


def _normalize_nested_state_map(raw: dict[Any, Any]) -> dict[str, Any] | object:
    values: dict[str, Any] = {}
    for key, item in raw.items():
        normalized_key = str(key).strip()
        if not normalized_key:
            continue
        if isinstance(item, (str, int, float, bool)) or item is None:
            values[normalized_key] = item
            continue
        if isinstance(item, datetime):
            values[normalized_key] = _normalize_datetime(item).isoformat().replace("+00:00", "Z")
            continue
        if isinstance(item, list):
            normalized_list = _normalize_nested_state_list(item)
            if normalized_list is _SKIP_VALUE:
                return _SKIP_VALUE
            values[normalized_key] = normalized_list
            continue
        if isinstance(item, dict):
            normalized_map = _normalize_nested_state_map(item)
            if normalized_map is _SKIP_VALUE:
                return _SKIP_VALUE
            values[normalized_key] = normalized_map
            continue
        return _SKIP_VALUE
    return values
