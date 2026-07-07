from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml

from src.core.config_loader import PROGRAMS_ROOT
from src.core.exceptions import ConfigError


@dataclass(frozen=True, slots=True)
class PlatformS7State:
    position: str
    recorded_at: datetime
    recorded_by: str | None = None
    justification: str | None = None


def get_platform_s7_state_path(*, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return programs_root / "platform_state.yaml"


def load_platform_s7_state(*, programs_root: Path = PROGRAMS_ROOT) -> PlatformS7State | None:
    path = get_platform_s7_state_path(programs_root=programs_root)
    if not path.exists():
        return None
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as error:
        raise ConfigError(f"Invalid YAML in {path}.") from error
    if not isinstance(document, dict):
        raise ConfigError(f"Expected mapping in {path}.")

    schema_version = _required_string(document.get("schema_version"), field_name="schema_version")
    if schema_version != "1.0":
        raise ConfigError(f"Unsupported platform S7 schema_version {schema_version!r} in {path}.")

    position = _required_string(document.get("position"), field_name="position")
    if position not in {"complete", "deferred"}:
        raise ConfigError(f"Platform S7 position in {path} must be 'complete' or 'deferred'.")
    justification = _optional_string(document.get("justification"), field_name="justification")
    if position == "deferred" and justification is None:
        raise ConfigError(f"Platform S7 deferred position in {path} requires a non-empty justification.")

    return PlatformS7State(
        position=position,
        recorded_at=_parse_datetime(document.get("recorded_at")),
        recorded_by=_load_optional_string(document.get("recorded_by"), field_name="recorded_by"),
        justification=_load_optional_string(document.get("justification"), field_name="justification"),
    )


def save_platform_s7_state(
    *,
    position: str,
    recorded_at: datetime,
    recorded_by: str | None,
    justification: str | None = None,
    programs_root: Path = PROGRAMS_ROOT,
) -> PlatformS7State:
    normalized_position = position.strip().lower()
    normalized_justification = _optional_string(justification, field_name="justification")
    normalized_recorded_at = _require_aware_datetime(recorded_at, field_name="recorded_at")
    if normalized_position not in {"complete", "deferred"}:
        raise ValueError("position must be 'complete' or 'deferred'.")
    if normalized_position == "deferred" and normalized_justification is None:
        raise ValueError("justification must be non-empty when position is 'deferred'.")
    if normalized_position == "complete":
        normalized_justification = None

    record = PlatformS7State(
        position=normalized_position,
        recorded_at=normalized_recorded_at,
        recorded_by=_optional_string(recorded_by, field_name="recorded_by"),
        justification=normalized_justification,
    )
    path = get_platform_s7_state_path(programs_root=programs_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "position": record.position,
                "recorded_at": record.recorded_at.isoformat(),
                "recorded_by": record.recorded_by,
                "justification": record.justification,
            },
            sort_keys=False,
            allow_unicode=False,
        ),
        encoding="utf-8",
    )
    return record


def _parse_datetime(value: object) -> datetime:
    if value in (None, ""):
        raise ConfigError("platform S7 state requires recorded_at.")
    if not isinstance(value, str):
        raise ConfigError("platform S7 recorded_at must be a string.")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ConfigError(f"Invalid datetime value {value!r} in platform S7 state.") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ConfigError("platform S7 recorded_at must include timezone information.")
    return parsed


def _require_aware_datetime(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include timezone information.")
    return value


def _required_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"platform S7 {field_name} must be a string.")
    return value


def _optional_string(value: object, *, field_name: str) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ConfigError(f"platform S7 {field_name} must be a string.")
    return value.strip() or None


def _load_optional_string(value: object, *, field_name: str) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ConfigError(f"platform S7 {field_name} must be a string.")
    if value != value.strip():
        raise ConfigError(f"platform S7 {field_name} must not contain surrounding whitespace.")
    return value
