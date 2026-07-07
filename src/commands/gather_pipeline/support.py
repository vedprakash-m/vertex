from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
import hashlib
from typing import Any

from src.ai.safety.pii_scrubber import filter_text
from src.commands.gather_pipeline.ado_pipeline_stage import _parse_datetime, _parse_float
from src.core.m365_payload_support import optional_string as _optional_string
from src.core.models import Confidence, WorkItem
from src.core.models_v2 import KustoQuery


def sanitize_discovery_result(result: Any, config: Any) -> Any:
    del config
    discovered_refs = []
    for discovered_ref in result.discovered_refs:
        registration = discovered_ref.registration
        if registration.ref_title:
            registration = replace(registration, ref_title=filter_text(registration.ref_title))
        discovered_refs.append(replace(discovered_ref, registration=registration))
    return replace(result, discovered_refs=tuple(discovered_refs))


def enrich_resources(resources: Any, workstream_map: dict[tuple[str, str], tuple[str, ...]]) -> Any:
    if hasattr(resources, "work_items"):
        enriched_work_items = tuple(enrich_work_item(item, workstream_map) for item in resources.work_items)
        freshness_items = resources.freshness_items
        enriched_freshness = None
        if freshness_items is not None:
            enriched_freshness = tuple(enrich_work_item(item, workstream_map) for item in freshness_items)

        enriched_prs = getattr(resources, "pull_requests", ())
        if enriched_prs:
            new_prs = []
            for pr in enriched_prs:
                ws_ids = workstream_map.get((pr.repository_id, "repository"))
                if ws_ids:
                    new_prs.append(replace(pr, workstream_ids=ws_ids))
                else:
                    new_prs.append(pr)
            enriched_prs = tuple(new_prs)

        return replace(
            resources,
            work_items=enriched_work_items,
            freshness_items=enriched_freshness,
            pull_requests=enriched_prs,
        )
    return resources


def enrich_work_item(item: WorkItem, workstream_map: dict[tuple[str, str], tuple[str, ...]]) -> WorkItem:
    workstream_ids = workstream_map.get((str(item.id), "work_item"))
    if workstream_ids is None:
        return item
    custom_fields = dict(item.custom_fields)
    custom_fields["workstream_ids"] = workstream_ids
    return replace(item, custom_fields=custom_fields)


def parse_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        return None


def coerce_datetime_or_none(value: Any) -> datetime | None:
    if value is None:
        return None
    return value if isinstance(value, datetime) else _parse_datetime(value)


def coerce_datetime(value: Any, *, fallback: datetime) -> datetime:
    parsed = value if isinstance(value, datetime) else _parse_datetime(value)
    if parsed is not None:
        return parsed
    return fallback


def hash_ingestion_query_text(text: str | None) -> str | None:
    normalized = _optional_string(text)
    if normalized is None:
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def build_captured_window(start: datetime | None, end: datetime | None) -> str | None:
    if start is None and end is None:
        return None
    start_text = start.astimezone(timezone.utc).isoformat() if start is not None else ""
    end_text = end.astimezone(timezone.utc).isoformat() if end is not None else ""
    return f"{start_text}/{end_text}"


def roll_query_value_history(previous_state: dict[str, Any] | None, numeric_value: float | None) -> list[float]:
    previous_values = (previous_state or {}).get("value_last_4")
    history: list[float] = []
    if isinstance(previous_values, list):
        for value in previous_values:
            try:
                history.append(float(value))
            except (TypeError, ValueError):
                continue
    if numeric_value is None:
        return history[-4:]
    history.append(float(numeric_value))
    return history[-4:]


def confidence_from_string(value: str) -> Confidence:
    normalized = value.strip().lower()
    if normalized == Confidence.HIGH.value:
        return Confidence.HIGH
    if normalized == Confidence.LOW.value:
        return Confidence.LOW
    if normalized == Confidence.NONE.value:
        return Confidence.NONE
    return Confidence.MEDIUM


def kusto_kpi_value(query: KustoQuery, row: dict[str, Any]) -> str | None:
    if not row:
        return None
    if query.render_as == "table" and query.result_column is None:
        return None
    if query.result_column is not None:
        value = row.get(query.result_column)
    else:
        value = next(iter(row.values()), None)
    if value is None:
        return None
    return str(value)


def kusto_event_timestamp(rows: list[dict[str, Any]], *, as_of: datetime) -> datetime:
    candidates: list[datetime] = []
    for row in rows:
        for key, value in row.items():
            normalized = key.strip().lower()
            if normalized not in {"timestamp", "date", "event_timestamp", "createdate", "resolveddate", "snapshot"}:
                continue
            parsed = _parse_datetime(value)
            if parsed is not None:
                candidates.append(parsed)
                continue
            parsed_date = parse_date(value)
            if parsed_date is not None:
                candidates.append(datetime.combine(parsed_date, time.min, tzinfo=timezone.utc))
    if candidates:
        return max(candidates)
    return datetime.combine(as_of.astimezone(timezone.utc).date(), time.min, tzinfo=timezone.utc)


def summarize_iteration_capacity(
    capacity_rows: tuple[dict[str, Any], ...],
) -> dict[str, int | float] | None:
    if not capacity_rows:
        return None

    team_member_count = 0
    members_with_capacity = 0
    total_capacity_per_day = 0.0
    days_off_entry_count = 0
    for row in capacity_rows:
        team_member_count += 1
        activities = row.get("activities")
        row_capacity = 0.0
        if isinstance(activities, list):
            for activity in activities:
                if not isinstance(activity, dict):
                    continue
                row_capacity += _parse_float(activity.get("capacityPerDay")) or 0.0
        if row_capacity > 0:
            members_with_capacity += 1
        total_capacity_per_day += row_capacity

        days_off = row.get("daysOff")
        if isinstance(days_off, list):
            days_off_entry_count += len([entry for entry in days_off if isinstance(entry, dict)])

    return {
        "team_member_count": team_member_count,
        "members_with_capacity": members_with_capacity,
        "total_capacity_per_day": round(total_capacity_per_day, 2),
        "days_off_entry_count": days_off_entry_count,
    }


def count_business_days_inclusive(start_date: date, end_date: date) -> int:
    if end_date < start_date:
        return 0

    business_days = 0
    current = start_date
    while current <= end_date:
        if current.weekday() < 5:
            business_days += 1
        current += timedelta(days=1)
    return business_days


def summarize_sprint_pace(
    start_date: date | None,
    finish_date: date | None,
    *,
    as_of: datetime,
    completion_pct: int,
) -> dict[str, int | str] | None:
    if start_date is None or finish_date is None or finish_date < start_date:
        return None

    total_business_days = count_business_days_inclusive(start_date, finish_date)
    if total_business_days <= 0:
        return None

    today = as_of.date()
    if today < start_date:
        elapsed_business_days = 0
        remaining_business_days = total_business_days
    elif today > finish_date:
        elapsed_business_days = total_business_days
        remaining_business_days = 0
    else:
        elapsed_business_days = count_business_days_inclusive(start_date, today)
        remaining_business_days = count_business_days_inclusive(today + timedelta(days=1), finish_date)

    expected_completion_pct = round((elapsed_business_days / total_business_days) * 100)
    pace_delta_pct = completion_pct - expected_completion_pct
    if abs(pace_delta_pct) <= 5:
        pace_status = "on_track"
        text = f"pace on track vs {expected_completion_pct}% elapsed"
    elif pace_delta_pct > 0:
        pace_status = "ahead"
        text = f"pace {pace_delta_pct}pts ahead of {expected_completion_pct}% elapsed"
    else:
        pace_status = "behind"
        text = f"pace {abs(pace_delta_pct)}pts behind {expected_completion_pct}% elapsed"

    return {
        "elapsed_business_days": elapsed_business_days,
        "total_business_days": total_business_days,
        "remaining_business_days": remaining_business_days,
        "expected_completion_pct": expected_completion_pct,
        "pace_delta_pct": pace_delta_pct,
        "pace_status": pace_status,
        "text": text,
    }


def summarize_sprint_throughput(
    *,
    committed_item_count: int,
    completed_item_count: int,
    open_item_count: int,
    pace_summary: dict[str, int | str] | None,
    completion_pct: int,
) -> dict[str, float | int | str] | None:
    if committed_item_count <= 0 or pace_summary is None:
        return None

    elapsed_business_days = int(pace_summary["elapsed_business_days"])
    remaining_business_days = int(pace_summary["remaining_business_days"])
    observed_completion_per_business_day = round(
        completed_item_count / max(elapsed_business_days, 1),
        2,
    ) if elapsed_business_days > 0 else 0.0
    required_completion_per_business_day = round(
        open_item_count / max(remaining_business_days, 1),
        2,
    ) if open_item_count > 0 else 0.0

    if open_item_count == 0:
        projected_completion_pct = 100
        projection_status = "complete"
        text = "sprint complete"
    elif remaining_business_days <= 0:
        projected_completion_pct = completion_pct
        projection_status = "at_risk"
        text = f"~{projected_completion_pct}% by close ({required_completion_per_business_day:.1f}/day needed)"
    else:
        projected_completed_count = completed_item_count + (
            observed_completion_per_business_day * remaining_business_days
        )
        projected_completion_pct = min(
            100,
            round((projected_completed_count / committed_item_count) * 100),
        )
        if projected_completion_pct >= 100:
            projection_status = "finish"
            text = (
                f"tracking to finish at {observed_completion_per_business_day:.1f}/day "
                f"({required_completion_per_business_day:.1f}/day needed)"
            )
        else:
            projection_status = "at_risk"
            text = (
                f"~{projected_completion_pct}% by close at {observed_completion_per_business_day:.1f}/day "
                f"({required_completion_per_business_day:.1f}/day needed)"
            )

    return {
        "observed_completion_per_business_day": observed_completion_per_business_day,
        "required_completion_per_business_day": required_completion_per_business_day,
        "projected_completion_pct": projected_completion_pct,
        "projection_status": projection_status,
        "text": text,
    }


def parse_iteration_date(iteration: dict[str, Any], key: str) -> date | None:
    attributes = iteration.get("attributes")
    if not isinstance(attributes, dict):
        return None
    raw_value = attributes.get(key)
    parsed = _parse_datetime(raw_value)
    if parsed is not None:
        return parsed.date()
    return parse_date(raw_value)


def iteration_contains_item(iteration_path: str, item_iteration_path: str) -> bool:
    normalized_iteration = iteration_path.strip().lower().rstrip("\\")
    normalized_item = item_iteration_path.strip().lower().rstrip("\\")
    return normalized_item == normalized_iteration or normalized_item.startswith(f"{normalized_iteration}\\")


def format_iteration_window(start_date: date | None, finish_date: date | None, *, as_of: datetime) -> str | None:
    if start_date is None and finish_date is None:
        return None
    if start_date is not None and finish_date is not None:
        days_remaining = (finish_date - as_of.date()).days
        if days_remaining > 0:
            return f"window {start_date.isoformat()} to {finish_date.isoformat()} ({days_remaining}d remaining)"
        if days_remaining == 0:
            return f"window {start_date.isoformat()} to {finish_date.isoformat()} (ends today)"
        return f"window {start_date.isoformat()} to {finish_date.isoformat()} ({abs(days_remaining)}d overdue)"
    if start_date is not None:
        return f"starts {start_date.isoformat()}"
    assert finish_date is not None
    return f"ends {finish_date.isoformat()}"
