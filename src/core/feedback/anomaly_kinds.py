from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from src.core.models_v2 import Signal


Severity = Literal["info", "warn", "alert"]


@dataclass(frozen=True, slots=True)
class RegisteredAnomalyKind:
    name: str
    severity_fn: Callable[[Signal], Severity]
    banner_fn: Callable[[Signal], str]


_REGISTRY: dict[str, RegisteredAnomalyKind] = {}


def register_anomaly_kind(
    name: str,
    severity_fn: Callable[[Signal], Severity],
    banner_fn: Callable[[Signal], str],
) -> None:
    normalized_name = name.strip().lower()
    if not normalized_name:
        raise ValueError("Anomaly kind name must not be empty.")
    _REGISTRY[normalized_name] = RegisteredAnomalyKind(
        name=normalized_name,
        severity_fn=severity_fn,
        banner_fn=banner_fn,
    )


def get_anomaly_kind(name: str) -> RegisteredAnomalyKind:
    normalized_name = name.strip().lower()
    try:
        return _REGISTRY[normalized_name]
    except KeyError as error:
        raise KeyError(f"Unknown catchup anomaly kind '{name}'.") from error


def registered_anomaly_kinds() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY))


def _severity_info(_signal: Signal) -> Severity:
    return "info"


def _severity_warn(_signal: Signal) -> Severity:
    return "warn"


def _eta_slip_banner(signal: Signal) -> str:
    return f"ETA slip: ADO#{_work_item_id(signal)} moved from {_metadata_value(signal, 'prior')} to {_metadata_value(signal, 'current')}."


def _eta_pull_in_banner(signal: Signal) -> str:
    return f"ETA change: ADO#{_work_item_id(signal)} moved from {_metadata_value(signal, 'prior')} to {_metadata_value(signal, 'current')}."


def _owner_change_banner(signal: Signal) -> str:
    return f"Owner change: ADO#{_work_item_id(signal)} moved from {_metadata_value(signal, 'prior')} to {_metadata_value(signal, 'current')}."


def _state_change_banner(signal: Signal) -> str:
    return f"State change: ADO#{_work_item_id(signal)} moved from {_metadata_value(signal, 'prior')} to {_metadata_value(signal, 'current')}."


def _generic_change_banner(signal: Signal) -> str:
    field_name = _metadata_value(signal, "field")
    return f"ADO#{_work_item_id(signal)} {field_name} changed from {_metadata_value(signal, 'prior')} to {_metadata_value(signal, 'current')}."


def _metadata_value(signal: Signal, key: str) -> str:
    metadata = signal.metadata or {}
    value = metadata.get(key)
    text = str(value).strip() if value is not None else ""
    return text or "unset"


def _work_item_id(signal: Signal) -> str:
    metadata = signal.metadata or {}
    value = metadata.get("work_item_id")
    return str(value).strip() if value is not None else "?"


register_anomaly_kind("eta_slip", _severity_warn, _eta_slip_banner)
register_anomaly_kind("eta_pull_in", _severity_info, _eta_pull_in_banner)
register_anomaly_kind("silent_owner_change", _severity_warn, _owner_change_banner)
register_anomaly_kind("state_change", _severity_info, _state_change_banner)
register_anomaly_kind("generic_change", _severity_info, _generic_change_banner)