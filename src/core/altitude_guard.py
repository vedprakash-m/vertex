from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from src.core.models import Confidence
from src.core.models_v2 import Signal
from src.core.trajectory_analyzer import DriftPattern


@dataclass(frozen=True, slots=True)
class AltitudeGuardResult:
    signals: tuple[Signal, ...]
    drift_patterns: tuple[DriftPattern, ...]


def apply_altitude_guard(
    *,
    altitude: str,
    signals: Iterable[Signal],
    drift_patterns: Iterable[DriftPattern],
    escalation_item_id: int | None = None,
) -> AltitudeGuardResult:
    normalized_altitude = altitude.strip().lower()
    signal_entries = tuple(signals)
    drift_entries = tuple(drift_patterns)

    if normalized_altitude == "street":
        return AltitudeGuardResult(signals=signal_entries, drift_patterns=drift_entries)
    if normalized_altitude == "helicopter":
        return AltitudeGuardResult(signals=signal_entries, drift_patterns=drift_entries)
    if normalized_altitude == "satellite":
        return AltitudeGuardResult(
            signals=tuple(signal for signal in signal_entries if _include_satellite_signal(signal)),
            drift_patterns=tuple(pattern for pattern in drift_entries if pattern.severity == "high"),
        )
    if normalized_altitude == "escalation":
        if escalation_item_id is None:
            raise ValueError("escalation altitude requires escalation_item_id")
        return AltitudeGuardResult(
            signals=tuple(signal for signal in signal_entries if _signal_matches_item(signal, escalation_item_id)),
            drift_patterns=tuple(pattern for pattern in drift_entries if pattern.work_item_id == escalation_item_id),
        )
    raise ValueError(f"Unsupported altitude: {altitude}")


def _include_satellite_signal(signal: Signal) -> bool:
    if _is_risk_escalation_signal(signal):
        return True
    if signal.confidence == Confidence.LOW:
        return False
    severity = _signal_severity(signal)
    if severity == "low":
        return False
    return True


def _is_risk_escalation_signal(signal: Signal) -> bool:
    if not signal.metadata:
        return False
    field = _metadata_string(signal, "field")
    if field is None:
        return False
    normalized_field = field.replace("_", "").replace(" ", "").lower()
    if normalized_field not in {"risk", "risklevel"}:
        return False
    prior = _risk_rank(_metadata_string(signal, "prior"))
    current = _risk_rank(_metadata_string(signal, "current"))
    return current > prior


def _signal_severity(signal: Signal) -> str | None:
    if not signal.metadata:
        return None
    severity = signal.metadata.get("severity")
    return _normalize_severity(severity)


def _normalize_severity(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        if value <= 1:
            return "high"
        if value == 2:
            return "medium"
        return "low"
    text = str(value).strip().lower()
    if not text:
        return None
    if text in {"high", "sev0", "sev1", "critical", "blocker"}:
        return "high"
    if text in {"medium", "moderate", "sev2"}:
        return "medium"
    if text in {"low", "minor", "sev3", "sev4"}:
        return "low"
    return None


def _signal_matches_item(signal: Signal, work_item_id: int) -> bool:
    if signal.metadata and signal.metadata.get("work_item_id") == work_item_id:
        return True
    marker = f"WI:{work_item_id}"
    return any(entity_ref.strip().upper() == marker for entity_ref in signal.entity_refs)


def _metadata_string(signal: Signal, key: str) -> str | None:
    if not signal.metadata:
        return None
    value = signal.metadata.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _risk_rank(value: str | None) -> int:
    if value is None:
        return -1
    normalized = value.strip().lower()
    order = {
        "none": 0,
        "green": 0,
        "low": 1,
        "medium": 2,
        "amber": 2,
        "high": 3,
        "red": 3,
        "critical": 4,
    }
    return order.get(normalized, -1)