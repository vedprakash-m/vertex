from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from src.core.exceptions import ConfigError
from src.core.journal import PROGRAMS_ROOT


@dataclass(frozen=True, slots=True)
class SignalRetentionPolicy:
    retention_days_by_source: dict[str, int]
    default_retention_days: int


def load_signal_retention_policy(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> SignalRetentionPolicy | None:
    program_path = programs_root / program_id / "program.yaml"
    if not program_path.exists():
        return None

    with program_path.open("r", encoding="utf-8") as handle:
        document = yaml.safe_load(handle) or {}
    if not isinstance(document, dict):
        raise ConfigError(f"Expected top-level mapping in {program_path}.")

    retention_value = document.get("retention_days")
    if retention_value is None:
        return None
    if isinstance(retention_value, int):
        if retention_value <= 0:
            raise ConfigError("retention_days default must be a positive integer.")
        return SignalRetentionPolicy(retention_days_by_source={}, default_retention_days=retention_value)
    if not isinstance(retention_value, dict):
        raise ConfigError("retention_days must be either a positive integer or a mapping of source -> days.")

    default_retention_days = 365
    retention_days_by_source: dict[str, int] = {}
    for raw_key, raw_days in retention_value.items():
        key = str(raw_key).strip()
        if not key:
            raise ConfigError("retention_days keys must be non-empty strings.")
        if not isinstance(raw_days, int) or raw_days <= 0:
            raise ConfigError(f"retention_days['{key}'] must be a positive integer.")
        if key == "default":
            default_retention_days = raw_days
        else:
            retention_days_by_source[key] = raw_days

    return SignalRetentionPolicy(
        retention_days_by_source=retention_days_by_source,
        default_retention_days=default_retention_days,
    )