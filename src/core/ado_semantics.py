from __future__ import annotations

from datetime import datetime, timezone
import os

from src.core.models import Comment, WorkItem


_VERTEX_COMMENT_PREFIXES = ("📊 Vertex", "📋 Vertex")
_VERTEX_COMMENT_SUFFIXES = ("— Vertex (automated vitality check)",)


def normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def alias_from_identity(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if "@" in text:
        return text.split("@", 1)[0].strip().lower() or None
    return text.split()[-1].strip().lower() or None


def item_owner_alias(item: WorkItem) -> str | None:
    return alias_from_identity(item.assigned_to_email or item.assigned_to)


def is_vertex_generated_comment(comment: Comment) -> bool:
    identities = _vertex_service_identities()
    author_values = {
        (comment.created_by or "").strip().lower(),
        (comment.created_by_email or "").strip().lower(),
    }
    if identities and any(value in identities for value in author_values if value):
        return True
    text = comment.text.strip()
    return any(text.startswith(prefix) for prefix in _VERTEX_COMMENT_PREFIXES) or any(
        text.endswith(suffix) for suffix in _VERTEX_COMMENT_SUFFIXES
    )


def is_meaningful_owner_comment(comment: Comment, owner_alias: str | None) -> bool:
    if owner_alias is None or is_vertex_generated_comment(comment):
        return False
    return alias_from_identity(comment.created_by_email or comment.created_by) == owner_alias


def is_meaningful_vitality_change(field_name: str) -> bool:
    normalized = field_name.strip().lower().replace("_", "")
    return (
        normalized.endswith("state")
        or normalized.endswith("targetdate")
        or normalized.endswith("assignedto")
        or normalized.endswith("riskcomment")
        or normalized.endswith("discussion")
        or normalized in {"risk", "risklevel"}
        or normalized.endswith("risklevel")
    )


def latest_meaningful_ado_update(item: WorkItem) -> datetime | None:
    owner_alias = item_owner_alias(item)
    candidates = [
        normalize_datetime(revision.changed_date)
        for revision in item.revisions
        if any(is_meaningful_vitality_change(field_name) for field_name in revision.fields_changed)
    ]
    candidates.extend(
        normalize_datetime(comment.created_date)
        for comment in item.comments
        if is_meaningful_owner_comment(comment, owner_alias)
    )
    if candidates:
        return max(candidates)

    for key in ("changed_date", "changed_at", "System.ChangedDate"):
        parsed = _coerce_datetime(item.custom_fields.get(key))
        if parsed is not None:
            return parsed
    return None


def _coerce_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return normalize_datetime(value)
    if isinstance(value, str):
        try:
            return normalize_datetime(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            return None
    return None


def _vertex_service_identities() -> set[str]:
    identities: set[str] = set()
    for key in (
        "VERTEX_SERVICE_IDENTITY",
        "VERTEX_SERVICE_IDENTITIES",
    ):
        raw = os.environ.get(key, "")
        for entry in raw.replace(";", ",").split(","):
            normalized = entry.strip().lower()
            if normalized:
                identities.add(normalized)
    return identities