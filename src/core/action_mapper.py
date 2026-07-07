from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from src.core.ado_proposal import ADOUpdateEntry, ADOUpdateProposal
from src.core.models_v2 import ActionItem, Workstream
from src.core.workstream_path_resolver import (
    resolve_workstream_id_strict_longest as _resolve_workstream_id,
)


@dataclass(frozen=True, slots=True)
class MappedActionWorkItem:
    work_item_id: int
    title: str
    area_path: str | None
    workstream_id: str | None
    assigned_to: str | None
    revision_id: int | None


@dataclass(frozen=True, slots=True)
class MeetingActionMapping:
    action: ActionItem
    resolved_workstream_id: str | None
    matched_items: tuple[MappedActionWorkItem, ...]
    missing_work_item_ids: tuple[int, ...]
    is_net_new: bool
    needs_owner: bool
    needs_due_date: bool


def map_actions_to_work_items(
    actions: tuple[ActionItem, ...],
    *,
    item_rows_by_id: Mapping[int, Mapping[str, Any]],
    workstreams: tuple[Workstream, ...],
) -> tuple[MeetingActionMapping, ...]:
    mappings: list[MeetingActionMapping] = []
    for action in actions:
        matched_items: list[MappedActionWorkItem] = []
        missing_item_ids: list[int] = []
        for work_item_id in action.linked_work_item_ids:
            row = item_rows_by_id.get(work_item_id)
            if row is None:
                missing_item_ids.append(work_item_id)
                continue
            area_path = _field_string(row, "System.AreaPath")
            matched_items.append(
                MappedActionWorkItem(
                    work_item_id=work_item_id,
                    title=_field_string(row, "System.Title") or f"WI:{work_item_id}",
                    area_path=area_path,
                    workstream_id=_resolve_workstream_id(area_path, workstreams),
                    assigned_to=_assigned_to_alias(_field_value(row, "System.AssignedTo")),
                    revision_id=_coerce_int(row.get("rev")) or _coerce_int(_field_value(row, "System.Rev")),
                )
            )

        resolved_workstream_id = action.workstream_id
        if resolved_workstream_id is None:
            for matched_item in matched_items:
                if matched_item.workstream_id is not None:
                    resolved_workstream_id = matched_item.workstream_id
                    break

        owner_alias = action.owner_alias.strip().lower()
        needs_owner = owner_alias in {"", "unknown", "tbd", "unassigned"}
        needs_due_date = action.due_date is None
        is_net_new = not matched_items
        mappings.append(
            MeetingActionMapping(
                action=action,
                resolved_workstream_id=resolved_workstream_id,
                matched_items=tuple(matched_items),
                missing_work_item_ids=tuple(missing_item_ids),
                is_net_new=is_net_new,
                needs_owner=needs_owner,
                needs_due_date=needs_due_date,
            )
        )
    return tuple(mappings)


def build_meeting_action_proposal(
    *,
    program_id: str,
    meeting_id: str,
    meeting_title: str | None,
    mappings: tuple[MeetingActionMapping, ...],
    created_at: datetime | None = None,
    ttl_hours: int = 72,
) -> ADOUpdateProposal:
    resolved_created_at = _ensure_utc(created_at or datetime.now(timezone.utc))
    proposal_id = f"meeting-action-{_safe_identifier(meeting_id)}"
    entries: list[ADOUpdateEntry] = []
    for mapping in mappings:
        if not mapping.matched_items:
            continue
        for matched_item in mapping.matched_items:
            entries.append(
                ADOUpdateEntry(
                    work_item_id=matched_item.work_item_id,
                    action="add_comment",
                    field_or_tag="comment",
                    current_value=None,
                    proposed_value=_build_meeting_action_comment(
                        meeting_id=meeting_id,
                        meeting_title=meeting_title,
                        mapping=mapping,
                        matched_item=matched_item,
                    ),
                    reason=f"Proposed from meeting-close transcript {meeting_title or meeting_id}.",
                    revision_id=matched_item.revision_id,
                )
            )
    return ADOUpdateProposal(
        id=proposal_id,
        program_id=program_id,
        edition_id=None,
        issue_number=None,
        update_type="meeting_action",
        created_at=resolved_created_at,
        expires_at=resolved_created_at + timedelta(hours=ttl_hours),
        entries=tuple(entries),
    )


def _build_meeting_action_comment(
    *,
    meeting_id: str,
    meeting_title: str | None,
    mapping: MeetingActionMapping,
    matched_item: MappedActionWorkItem,
) -> str:
    owner_label = mapping.action.owner_alias if not mapping.needs_owner else "needs owner"
    due_label = mapping.action.due_date.isoformat() if mapping.action.due_date is not None else "needs date"
    lines = [
        f"Vertex meeting-close draft - {meeting_title or meeting_id}",
        "",
        f"Action: {mapping.action.text}",
        f"Owner: {owner_label}",
        f"Due: {due_label}",
        f"Workstream: {mapping.resolved_workstream_id or matched_item.workstream_id or '-'}",
        "",
        "Please confirm this meeting action is captured and update the item as needed.",
        "",
        "Vertex (meeting-close draft)",
    ]
    return "\n".join(lines)
def _field_value(row: Mapping[str, Any], field_name: str) -> Any:
    fields = row.get("fields") if isinstance(row.get("fields"), Mapping) else {}
    return fields.get(field_name)  # type: ignore[union-attr]


def _field_string(row: Mapping[str, Any], field_name: str) -> str | None:
    value = _field_value(row, field_name)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _assigned_to_alias(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        for key in ("uniqueName", "mail", "displayName", "name"):
            candidate = value.get(key)
            if candidate not in (None, ""):
                return _normalize_alias(candidate)
        return None
    return _normalize_alias(value)


def _normalize_alias(value: Any) -> str | None:
    text = str(value).strip().lower()
    if not text:
        return None
    if "@" in text:
        text = text.split("@", 1)[0]
    normalized = "".join(character for character in text if character.isalnum() or character in {".", "_", "-"})
    return normalized or None


def _coerce_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _ensure_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc) if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _safe_identifier(value: str) -> str:
    normalized = value.strip().lower()
    safe = "".join(character if character.isalnum() else "-" for character in normalized)
    while "--" in safe:
        safe = safe.replace("--", "-")
    safe = safe.strip("-")
    return safe or "meeting"
