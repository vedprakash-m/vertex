from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any, Literal, cast

from src.core.catchup_scan import PROGRAMS_ROOT, WatchPollResult
from src.core.models import Confidence
from src.core.models_v2 import CatchupEvent


@dataclass(frozen=True, slots=True)
class CatchupState:
    last_catchup_at: datetime
    last_catchup_source: str
    last_scan_cursor_ado: datetime
    last_result: WatchPollResult | None = None


def get_catchup_state_path(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return programs_root / program_id / "_feedback" / "last_session_at.json"


def get_catchup_lock_path(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return programs_root / program_id / "_feedback" / ".catchup.lock"


def load_catchup_state(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> CatchupState | None:
    path = get_catchup_state_path(program_id, programs_root=programs_root)
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("catchup state payload must be a mapping")
    last_catchup_at = _required_datetime(payload.get("last_catchup_at"), field_name="last_catchup_at")
    last_catchup_source = _required_non_empty_string(
        payload.get("last_catchup_source"),
        field_name="last_catchup_source",
    )
    cursor = _require_mapping(payload.get("last_scan_cursor"), field_name="last_scan_cursor")
    cursor_ado = _required_datetime(cursor.get("ado"), field_name="last_scan_cursor.ado")
    last_result_payload = payload.get("last_result")
    return CatchupState(
        last_catchup_at=last_catchup_at,
        last_catchup_source=last_catchup_source,
        last_scan_cursor_ado=cursor_ado,
        last_result=_poll_result_from_payload(program_id, last_result_payload),
    )


def write_catchup_state(
    program_id: str,
    state: CatchupState,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> Path:
    path = get_catchup_state_path(program_id, programs_root=programs_root)
    payload = {
        "schema_version": "1.0",
        "last_catchup_at": _ensure_utc(state.last_catchup_at).isoformat(),
        "last_catchup_source": state.last_catchup_source,
        "last_scan_cursor": {
            "ado": _ensure_utc(state.last_scan_cursor_ado).isoformat(),
        },
        "last_result": _poll_result_to_payload(state.last_result),
    }
    _write_atomic_json(path, payload)
    return path


def _poll_result_to_payload(result: WatchPollResult | None) -> dict[str, Any] | None:
    if result is None:
        return None
    return {
        "since": _ensure_utc(result.since).isoformat(),
        "polled_at": _ensure_utc(result.polled_at).isoformat(),
        "scanned_items": result.scanned_items,
        "discovered_signals": result.discovered_signals,
        "new_signals": result.new_signals,
        "auto_reviews_written": result.auto_reviews_written,
        "trajectory_updates": result.trajectory_updates,
        "ado_calls": result.ado_calls,
        "new_signal_summaries": list(result.new_signal_summaries),
        "total_changed_items": result.total_changed_items,
        "catchup_events": [_catchup_event_to_payload(event) for event in result.catchup_events],
    }


def _poll_result_from_payload(program_id: str, payload: Any) -> WatchPollResult | None:
    if payload is None:
        return None
    raw_payload = _require_mapping(payload, field_name="last_result")
    return WatchPollResult(
        program_id=program_id,
        since=_required_datetime(raw_payload.get("since"), field_name="last_result.since"),
        polled_at=_required_datetime(raw_payload.get("polled_at"), field_name="last_result.polled_at"),
        scanned_items=_required_int(raw_payload.get("scanned_items"), field_name="scanned_items"),
        discovered_signals=_required_int(raw_payload.get("discovered_signals"), field_name="discovered_signals"),
        new_signals=_required_int(raw_payload.get("new_signals"), field_name="new_signals"),
        auto_reviews_written=_required_int(raw_payload.get("auto_reviews_written"), field_name="auto_reviews_written"),
        trajectory_updates=_required_int(raw_payload.get("trajectory_updates"), field_name="trajectory_updates"),
        ado_calls=_required_int(raw_payload.get("ado_calls"), field_name="ado_calls"),
        new_signal_summaries=_required_string_tuple(
            raw_payload.get("new_signal_summaries"),
            field_name="new_signal_summaries",
        ),
        total_changed_items=_optional_int(raw_payload.get("total_changed_items"), field_name="total_changed_items"),
        catchup_events=_catchup_events_from_payload(raw_payload.get("catchup_events")),
    )


def _catchup_event_to_payload(event: CatchupEvent) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "program_id": event.program_id,
        "detected_at": _ensure_utc(event.detected_at).isoformat(),
        "kind": event.kind,
        "work_item_id": event.work_item_id,
        "workstream_id": event.workstream_id,
        "summary": event.summary,
        "severity": event.severity,
        "salience_score": event.salience_score,
        "confidence": event.confidence.value,
        "signal_id": event.signal_id,
    }


def _catchup_events_from_payload(payload: Any) -> tuple[CatchupEvent, ...]:
    if payload is None:
        return ()
    if not isinstance(payload, list):
        raise TypeError("catchup_events must be a list")
    events: list[CatchupEvent] = []
    for raw_event in payload:
        if not isinstance(raw_event, dict):
            raise TypeError("catchup_events entries must be mappings")
        detected_at = _required_datetime(raw_event.get("detected_at"), field_name="detected_at")
        confidence = _required_confidence(raw_event.get("confidence"), field_name="confidence")
        events.append(
            CatchupEvent(
                event_id=_required_string(raw_event.get("event_id"), field_name="event_id").strip(),
                program_id=_required_string(raw_event.get("program_id"), field_name="program_id").strip(),
                detected_at=detected_at,
                kind=_required_string(raw_event.get("kind"), field_name="kind").strip(),
                work_item_id=_optional_int(raw_event.get("work_item_id"), field_name="work_item_id"),
                workstream_id=_optional_string(raw_event.get("workstream_id"), field_name="workstream_id"),
                summary=_required_string(raw_event.get("summary"), field_name="summary").strip(),
                severity=_required_severity(raw_event.get("severity")),
                salience_score=_required_float(raw_event.get("salience_score"), field_name="salience_score"),
                confidence=confidence,
                signal_id=_optional_string(raw_event.get("signal_id"), field_name="signal_id"),
            )
        )
    return tuple(events)


def _parse_optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_confidence(value: Any) -> Confidence | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return Confidence(value.strip())
    except ValueError:
        return None


def _required_string(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    return value


def _required_non_empty_string(value: Any, *, field_name: str) -> str:
    text = _required_string(value, field_name=field_name).strip()
    if not text:
        raise ValueError(f"{field_name} must not be empty")
    return text


def _required_int(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer")
    return value


def _optional_int(value: Any, *, field_name: str) -> int | None:
    if value is None:
        return None
    return _required_int(value, field_name=field_name)


def _optional_string(value: Any, *, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_string(value, field_name=field_name)


def _required_float(value: Any, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field_name} must be numeric")
    return float(value)


def _required_datetime(value: Any, *, field_name: str) -> datetime:
    parsed = _parse_datetime(value)
    if parsed is None:
        raise TypeError(f"{field_name} must be an ISO timestamp string")
    return parsed


def _required_confidence(value: Any, *, field_name: str) -> Confidence:
    parsed = _parse_confidence(value)
    if parsed is None:
        raise TypeError(f"{field_name} must be a confidence string")
    return parsed


def _required_severity(value: Any) -> Literal["info", "warn", "alert"]:
    severity = _required_string(value, field_name="severity").strip()
    if severity not in {"info", "warn", "alert"}:
        raise ValueError("severity must be one of: info, warn, alert")
    return cast(Literal["info", "warn", "alert"], severity)


def _required_string_tuple(value: Any, *, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise TypeError(f"{field_name} must be a list")
    result: list[str] = []
    for entry in value:
        if not isinstance(entry, str):
            raise TypeError(f"{field_name} entries must be strings")
        if entry.strip():
            result.append(entry)
    return tuple(result)


def _write_atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return _ensure_utc(datetime.fromisoformat(value))
    except ValueError:
        return None


def _require_mapping(value: Any, *, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{field_name} must be a mapping")
    return value


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)