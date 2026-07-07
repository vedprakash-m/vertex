from __future__ import annotations

from datetime import datetime, timezone

from src.core.ado_semantics import is_vertex_generated_comment
from src.core.models import WorkItem


def has_response_since(item: WorkItem, since: datetime) -> bool:
    last_response_activity = get_last_response_activity(item)
    return last_response_activity is not None and last_response_activity > _normalize_datetime(since)


def get_last_response_activity(item: WorkItem) -> datetime | None:
    candidates: list[datetime] = []
    for revision in item.revisions:
        if any(_is_response_change(field_name) for field_name in revision.fields_changed):
            candidates.append(_normalize_datetime(revision.changed_date))
    for comment in item.comments:
        if not is_vertex_generated_comment(comment):
            candidates.append(_normalize_datetime(comment.created_date))
    return max(candidates) if candidates else None


def _is_response_change(field_name: str) -> bool:
    normalized = field_name.lower()
    return (
        _is_state_change(field_name)
        or normalized.endswith("riskcomment")
        or normalized.endswith("discussion")
        or normalized.endswith("description")
    )


def _is_state_change(field_name: str) -> bool:
    normalized = field_name.lower()
    return normalized == "system.state" or normalized.endswith(".state")


def _normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)