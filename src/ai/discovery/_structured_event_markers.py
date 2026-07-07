from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from src.ai._pipeline import AIPipelineError, process_generated_text


class StructuredEventMarkerError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class StructuredEventMarkerParseResult:
    event_type: str
    payload: dict[str, Any]
    occurred_at: datetime | None = None
    temporal_confidence: str | None = None


def parse_structured_event_marker(line: str) -> tuple[str, dict[str, Any]] | None:
    parsed = parse_structured_event_marker_result(line)
    if parsed is None:
        return None
    return parsed.event_type, parsed.payload


def parse_structured_event_marker_result(line: str) -> StructuredEventMarkerParseResult | None:
    lowered = line.lower()
    if lowered.startswith("decision:"):
        payload, occurred_at, temporal_confidence = _parse_decision_line(line)
        return StructuredEventMarkerParseResult(
            event_type="decision.made.v1",
            payload=payload,
            occurred_at=occurred_at,
            temporal_confidence=temporal_confidence,
        )
    if lowered.startswith("risk:"):
        payload, occurred_at, temporal_confidence = _parse_risk_line(line)
        return StructuredEventMarkerParseResult(
            event_type="risk.raised.v1",
            payload=payload,
            occurred_at=occurred_at,
            temporal_confidence=temporal_confidence,
        )
    if lowered.startswith("milestone:"):
        event_type, payload, occurred_at, temporal_confidence = _parse_milestone_line(line)
        return StructuredEventMarkerParseResult(
            event_type=event_type,
            payload=payload,
            occurred_at=occurred_at,
            temporal_confidence=temporal_confidence,
        )
    if lowered.startswith("metric:"):
        payload, occurred_at, temporal_confidence = _parse_metric_line(line, marker="Metric:")
        return StructuredEventMarkerParseResult(
            event_type="metric.observed.v1",
            payload=payload,
            occurred_at=occurred_at,
            temporal_confidence=temporal_confidence,
        )
    if lowered.startswith("kpi:"):
        payload, occurred_at, temporal_confidence = _parse_metric_line(line, marker="KPI:")
        return StructuredEventMarkerParseResult(
            event_type="metric.observed.v1",
            payload=payload,
            occurred_at=occurred_at,
            temporal_confidence=temporal_confidence,
        )
    return None


def parse_marker_segments(line: str, *, marker: str) -> dict[str, str]:
    body = line[len(marker) :].strip()
    if not body:
        raise StructuredEventMarkerError(f"{marker} marker is empty.")
    parts = [part.strip() for part in body.split("|") if part.strip()]
    if not parts:
        raise StructuredEventMarkerError(f"{marker} marker is empty.")
    payload: dict[str, str] = {}
    if "=" not in parts[0]:
        payload["text"] = parts[0]
        parts = parts[1:]
    for part in parts:
        if "=" not in part:
            raise StructuredEventMarkerError(f"Structured marker segment is missing '=': {part}")
        key, value = part.split("=", 1)
        payload[key.strip().lower()] = value.strip()
    return payload


def entity_resolution_from_payload(entity_ref_fields: frozenset[str], payload: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    resolutions: list[dict[str, Any]] = []
    for field_name in entity_ref_fields:
        raw_value = payload.get(field_name)
        if isinstance(raw_value, str):
            resolutions.append(
                {
                    "raw_name": raw_value,
                    "resolved_entity_id": raw_value,
                    "match_kind": "exact_id",
                    "score": 1.0,
                }
            )
        elif isinstance(raw_value, list):
            for item in raw_value:
                if isinstance(item, str):
                    resolutions.append(
                        {
                            "raw_name": item,
                            "resolved_entity_id": item,
                            "match_kind": "exact_id",
                            "score": 1.0,
                        }
                    )
    return tuple(resolutions)


def require_field(payload: dict[str, str], field_name: str) -> str:
    value = payload.get(field_name)
    if value is None or not value.strip():
        raise StructuredEventMarkerError(f"Structured marker missing required field: {field_name}")
    return value.strip()


def safe_text(value: str, *, field_name: str) -> str:
    try:
        return process_generated_text(value).text
    except AIPipelineError as error:
        raise StructuredEventMarkerError(f"Unsafe marker field '{field_name}': {error}") from error


def safe_optional_text(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    return safe_text(value, field_name="optional_text")


def parse_csv_list(value: str) -> list[str]:
    values = [item.strip() for item in value.split(",") if item.strip()]
    if not values:
        raise StructuredEventMarkerError("List-valued marker field must contain at least one item.")
    return values


def parse_dimension_map(value: str) -> dict[str, str]:
    dimensions: dict[str, str] = {}
    for item in value.split(","):
        stripped = item.strip()
        if not stripped:
            continue
        if ":" not in stripped:
            raise StructuredEventMarkerError(f"Dimension-valued marker field must use key:value pairs: {stripped}")
        key, raw_value = stripped.split(":", 1)
        dimension_key = key.strip()
        dimension_value = raw_value.strip()
        if not dimension_key or not dimension_value:
            raise StructuredEventMarkerError(f"Dimension-valued marker field must use key:value pairs: {stripped}")
        dimensions[dimension_key] = dimension_value
    if not dimensions:
        raise StructuredEventMarkerError("Dimension-valued marker field must contain at least one key:value pair.")
    return dimensions


def parse_float(value: str, *, field_name: str) -> float:
    try:
        return float(value)
    except ValueError as error:
        raise StructuredEventMarkerError(f"Structured marker field '{field_name}' must be numeric.") from error


def marker_occurrence_or_default(
    parsed: StructuredEventMarkerParseResult,
    *,
    default_occurred_at: datetime,
    default_temporal_confidence: str,
) -> tuple[datetime, str]:
    if parsed.occurred_at is not None and parsed.temporal_confidence is not None:
        return parsed.occurred_at, parsed.temporal_confidence
    return default_occurred_at, default_temporal_confidence


def _parse_decision_line(line: str) -> tuple[dict[str, Any], datetime | None, str | None]:
    segments = parse_marker_segments(line, marker="Decision:")
    occurred_at, temporal_confidence = _pop_temporal_hint(segments)
    decision_text = safe_text(segments.pop("text", ""), field_name="decision_text")
    title = safe_text(require_field(segments, "title"), field_name="title")
    decided_by = parse_csv_list(require_field(segments, "decided_by"))
    return (
        {
            "decision_id": require_field(segments, "decision_id"),
            "title": title,
            "decision_text": decision_text,
            "decided_by": decided_by,
            "forum": safe_optional_text(segments.get("forum")),
        },
        occurred_at,
        temporal_confidence,
    )


def _parse_risk_line(line: str) -> tuple[dict[str, Any], datetime | None, str | None]:
    segments = parse_marker_segments(line, marker="Risk:")
    occurred_at, temporal_confidence = _pop_temporal_hint(segments)
    description = safe_text(segments.pop("text", ""), field_name="description")
    payload: dict[str, Any] = {
        "risk_id": require_field(segments, "risk_id"),
        "title": safe_text(require_field(segments, "title"), field_name="title"),
        "severity": require_field(segments, "severity"),
    }
    if description:
        payload["description"] = description
    if owner_person_id := segments.get("owner_person_id"):
        payload["owner_person_id"] = owner_person_id
    if workstream_id := segments.get("workstream_id"):
        payload["workstream_id"] = workstream_id
    if likelihood := segments.get("likelihood"):
        payload["likelihood"] = likelihood
    return payload, occurred_at, temporal_confidence


def _parse_milestone_line(line: str) -> tuple[str, dict[str, Any], datetime | None, str | None]:
    segments = parse_marker_segments(line, marker="Milestone:")
    milestone_kind = require_field(segments, "kind").lower()
    milestone_id = require_field(segments, "milestone_id")
    occurred_at, temporal_confidence = _pop_temporal_hint(segments)
    if milestone_kind == "created":
        payload: dict[str, Any] = {
            "milestone_id": milestone_id,
            "name": safe_text(require_field(segments, "name"), field_name="name"),
            "target_date": require_field(segments, "target_date"),
        }
        if workstream_id := segments.get("workstream_id"):
            payload["workstream_id"] = workstream_id
        return "milestone.created.v1", payload, occurred_at, temporal_confidence
    if milestone_kind == "revised":
        payload = {
            "milestone_id": milestone_id,
            "new_target_date": require_field(segments, "new_target_date"),
        }
        if prior_target_date := segments.get("prior_target_date"):
            payload["prior_target_date"] = prior_target_date
        if reason := segments.get("reason"):
            payload["reason"] = safe_text(reason, field_name="reason")
        return "milestone.date_revised.v1", payload, occurred_at, temporal_confidence
    if milestone_kind == "completed":
        payload = {
            "milestone_id": milestone_id,
            "completed_on": require_field(segments, "completed_on"),
        }
        if evidence := segments.get("evidence"):
            payload["evidence"] = safe_text(evidence, field_name="evidence")
        if occurred_at is None:
            occurred_at, temporal_confidence = _parse_temporal_value(payload["completed_on"])
        return "milestone.completed.v1", payload, occurred_at, temporal_confidence
    raise StructuredEventMarkerError(f"Unsupported milestone marker kind: {milestone_kind}")


def _parse_metric_line(line: str, *, marker: str) -> tuple[dict[str, Any], datetime | None, str | None]:
    segments = parse_marker_segments(line, marker=marker)
    occurred_at, temporal_confidence = _pop_temporal_hint(segments)
    payload: dict[str, Any] = {
        "kpi_id": require_field(segments, "kpi_id"),
        "value": parse_float(require_field(segments, "value"), field_name="value"),
    }
    if unit := segments.get("unit"):
        payload["unit"] = safe_text(unit, field_name="unit")
    if window_start := segments.get("window_start"):
        payload["window_start"] = require_field(segments, "window_start")
    if window_end := segments.get("window_end"):
        payload["window_end"] = require_field(segments, "window_end")
    if dimensions := segments.get("dimensions"):
        payload["dimensions"] = parse_dimension_map(dimensions)
    if occurred_at is None and "window_end" in payload:
        occurred_at, temporal_confidence = _parse_temporal_value(str(payload["window_end"]))
    return payload, occurred_at, temporal_confidence


def _pop_temporal_hint(segments: dict[str, str]) -> tuple[datetime | None, str | None]:
    for field_name in ("occurred_at", "observed_at", "occurred_on", "observed_on"):
        value = segments.pop(field_name, None)
        if value is None or not value.strip():
            continue
        return _parse_temporal_value(value)
    return None, None


def _parse_temporal_value(value: str) -> tuple[datetime, str]:
    stripped = value.strip()
    if not stripped:
        raise StructuredEventMarkerError("Temporal marker field cannot be empty.")
    try:
        parsed_date = date.fromisoformat(stripped)
    except ValueError:
        parsed_date = None
    if parsed_date is not None and "T" not in stripped.upper():
        return datetime.combine(parsed_date, datetime.min.time(), tzinfo=timezone.utc), "approximate"

    normalized = stripped.replace("Z", "+00:00")
    try:
        parsed_datetime = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise StructuredEventMarkerError(f"Temporal marker field must be ISO-8601 date/datetime: {value}") from error
    if parsed_datetime.tzinfo is None:
        return parsed_datetime.replace(tzinfo=timezone.utc), "approximate"
    return parsed_datetime.astimezone(timezone.utc), "exact"
