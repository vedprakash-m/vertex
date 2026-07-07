from __future__ import annotations

from dataclasses import replace

from src.core.models_v2 import Signal, SignalClass

_DECISION_HINTS = ("decision", "decided", "approved", "agreed to", "commit to", "committed to", "leadership ask")
_DEPENDENCY_HINTS = ("blocked by", "depends on", "dependency", "dependent on", "waiting on", "pending from")
_RISK_HINTS = ("at risk", "risk", "blocker", "blocked", "concern", "slip", "slipped", "delay", "delayed")
_MITIGATION_HINTS = ("mitigation", "mitigate", "next step", "follow up", "workaround", "plan:", "action:")
_RCA_HINTS = ("root cause", "rca", "because", "due to", "caused by", "investigation found")
_SOURCE_DEFAULTS = {
    "icm": SignalClass.RISK,
    "vertex/freshness": SignalClass.RISK,
    "kusto": SignalClass.STATUS,
    "kusto_kpi": SignalClass.STATUS,
}


def classify_signal(signal: Signal) -> Signal:
    metadata = dict(signal.metadata or {})
    if _metadata_signal_class(metadata) is not None:
        return signal if signal.metadata is not None else replace(signal, metadata=metadata)
    metadata["signal_class"] = signal_class(signal).value
    return replace(signal, metadata=metadata)


def signal_class(signal: Signal) -> SignalClass:
    metadata_class = _metadata_signal_class(signal.metadata or {})
    if metadata_class is not None:
        return metadata_class

    if signal.source in _SOURCE_DEFAULTS:
        return _SOURCE_DEFAULTS[signal.source]

    lowered = signal.text.lower()
    if _contains_any(lowered, _DEPENDENCY_HINTS):
        return SignalClass.DEPENDENCY
    if _contains_any(lowered, _DECISION_HINTS):
        return SignalClass.DECISION
    if _contains_any(lowered, _RISK_HINTS):
        return SignalClass.RISK
    if _contains_any(lowered, _MITIGATION_HINTS):
        return SignalClass.MITIGATION
    if _contains_any(lowered, _RCA_HINTS):
        return SignalClass.RCA
    return SignalClass.STATUS


def _metadata_signal_class(metadata: dict[str, object]) -> SignalClass | None:
    raw_value = metadata.get("signal_class")
    if not isinstance(raw_value, str) or not raw_value.strip():
        return None
    try:
        return SignalClass.from_string(raw_value)
    except ValueError:
        return None


def _contains_any(text: str, hints: tuple[str, ...]) -> bool:
    return any(hint in text for hint in hints)
