from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from uuid import NAMESPACE_URL, uuid5

from src.core.feedback.anomaly_kinds import get_anomaly_kind
from src.core.models_v2 import CatchupEvent, Signal


DEFAULT_SALIENCE_SCORE = 0.5


def classify_catchup_signals(
    signals: tuple[Signal, ...],
    *,
    salience_weights: Mapping[str, float] | None = None,
) -> tuple[CatchupEvent, ...]:
    return tuple(
        _classify_signal(signal, salience_weights=salience_weights or {})
        for signal in signals
    )


def build_catchup_events(
    signals: tuple[Signal, ...],
    *,
    salience_weights: Mapping[str, float] | None = None,
    limit: int | None = None,
) -> tuple[CatchupEvent, ...]:
    events = classify_catchup_signals(signals, salience_weights=salience_weights)
    ordered = sorted(
        events,
        key=lambda event: (_severity_rank(event.severity), event.salience_score, event.detected_at),
        reverse=True,
    )
    if limit is None:
        return tuple(ordered)
    return tuple(ordered[: max(limit, 0)])


def build_catchup_summaries(
    signals: tuple[Signal, ...],
    *,
    salience_weights: Mapping[str, float] | None = None,
    limit: int = 3,
) -> tuple[str, ...]:
    events = build_catchup_events(signals, salience_weights=salience_weights, limit=limit)
    return tuple(event.summary for event in events)


def _classify_signal(signal: Signal, *, salience_weights: Mapping[str, float]) -> CatchupEvent:
    kind_name = _infer_kind(signal)
    kind = get_anomaly_kind(kind_name)
    work_item_id = _coerce_work_item_id(signal)
    return CatchupEvent(
        event_id=str(uuid5(NAMESPACE_URL, f"{signal.program_id}|{signal.id}|{kind_name}")),
        program_id=signal.program_id,
        detected_at=signal.timestamp,
        kind=kind_name,
        work_item_id=work_item_id,
        workstream_id=signal.workstream_id,
        summary=kind.banner_fn(signal),
        severity=kind.severity_fn(signal),
        salience_score=_salience_for_signal(signal, salience_weights),
        confidence=signal.confidence,
        signal_id=signal.id,
    )


def _severity_rank(severity: str) -> int:
    return {"info": 1, "warn": 2, "alert": 3}.get(severity, 0)


def _salience_for_signal(signal: Signal, salience_weights: Mapping[str, float]) -> float:
    if signal.workstream_id is None:
        return DEFAULT_SALIENCE_SCORE
    try:
        return float(salience_weights.get(signal.workstream_id, DEFAULT_SALIENCE_SCORE))
    except (TypeError, ValueError):
        return DEFAULT_SALIENCE_SCORE


def _coerce_work_item_id(signal: Signal) -> int | None:
    metadata = signal.metadata or {}
    value = metadata.get("work_item_id")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _infer_kind(signal: Signal) -> str:
    metadata = signal.metadata or {}
    field_name = str(metadata.get("field") or "").strip()
    if field_name == "TargetDate":
        prior = _parse_date_like(metadata.get("prior"))
        current = _parse_date_like(metadata.get("current"))
        if prior is not None and current is not None and current > prior:
            return "eta_slip"
        return "eta_pull_in"
    if field_name == "AssignedTo":
        return "silent_owner_change"
    if field_name in {"State", "System.State"}:
        return "state_change"
    return "generic_change"


def _parse_date_like(value: object) -> date | None:
    text = str(value).strip() if value is not None else ""
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    for parser in (_parse_iso_datetime, _parse_iso_date, _parse_month_day):
        parsed = parser(normalized)
        if parsed is not None:
            return parsed
    return None


def _parse_iso_datetime(value: str) -> date | None:
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return None


def _parse_iso_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _parse_month_day(value: str) -> date | None:
    for pattern in ("%b %d", "%B %d"):
        try:
            parsed = datetime.strptime(value, pattern)
            return date(2000, parsed.month, parsed.day)
        except ValueError:
            continue
    return None