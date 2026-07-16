from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import portalocker

from src.core.jsonl_utils import (
    append_jsonl_line,
    read_jsonl_records,
    validate_jsonl_row,
    write_checksum_file,
)
from src.core.models_v2 import ActionItem, ActionSourceType, ActionStatus, ActionStatusUpdate, TrajectoryPoint
from src.core.operation_trace import REF_TYPE_FACT, record_trace_link
from src.core.store_factory import build_trajectory_store_for_program_id


REPO_ROOT = Path(__file__).resolve().parents[2]
PROGRAMS_ROOT = REPO_ROOT / "programs"

# High-risk append-only file — grows with every action and every status update.
# Rotated at 10 MB (spec §11.3 Phase 5 / D-23) to bound on-disk footprint.
_ACTIONS_MAX_BYTES = 10 * 1024 * 1024

ActionLogEntry = ActionItem | ActionStatusUpdate
_ACTIVE_ACTION_STATUSES = {ActionStatus.OPEN, ActionStatus.IN_PROGRESS}
_RESOLVED_ACTION_STATUSES = {ActionStatus.DONE, ActionStatus.CANCELLED}


def get_actions_path(program_id: str, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return programs_root / program_id / "journal" / "actions.jsonl"


def build_action_id(
    program_id: str,
    *,
    text: str,
    owner_alias: str,
    due_date: date | None,
    source_signal_id: str | None,
    workstream_id: str | None,
    linked_work_item_ids: tuple[int, ...] = (),
) -> str:
    seed = "|".join(
        (
            "action",
            program_id.strip().lower(),
            " ".join(text.strip().lower().split()),
            owner_alias.strip().lower(),
            due_date.isoformat() if due_date is not None else "",
            workstream_id or "",
            ",".join(str(work_item_id) for work_item_id in linked_work_item_ids),
        )
    )
    return str(uuid5(NAMESPACE_URL, seed))


def append_action(
    program_id: str, action: ActionItem, programs_root: Path = PROGRAMS_ROOT, *, correlation_id: str = "",
) -> Path:
    _sync_action_fact(
        program_id, action, recorded_at=action.created_at, programs_root=programs_root, correlation_id=correlation_id,
    )
    target = get_actions_path(program_id, programs_root)
    payload = json.dumps(_action_to_record(action), separators=(",", ":")) + os.linesep
    append_jsonl_line(target, payload, max_bytes=_ACTIONS_MAX_BYTES)
    return target


def read_action_log(program_id: str, programs_root: Path = PROGRAMS_ROOT) -> tuple[ActionLogEntry, ...]:
    path = get_actions_path(program_id, programs_root)
    if not path.exists():
        return ()
    entries: list[ActionLogEntry] = []
    for record in read_jsonl_records(path):
        raw_record_type = record.get("record_type", "action")
        if not isinstance(raw_record_type, str):
            raise TypeError("record_type must be a string")
        record_type = raw_record_type
        if record_type == "status_update":
            entries.append(_status_update_from_record(record))
        elif record_type == "action":
            entries.append(_action_from_record(record))
        else:
            raise ValueError(f"Unknown action log record_type '{record_type}'")
    return tuple(entries)


def load_latest_action_statuses(program_id: str, programs_root: Path = PROGRAMS_ROOT) -> dict[str, ActionStatusUpdate]:
    latest: dict[str, ActionStatusUpdate] = {}
    for entry in read_action_log(program_id, programs_root):
        if isinstance(entry, ActionStatusUpdate):
            latest[entry.action_id] = entry
    return latest


def load_actions(program_id: str, programs_root: Path = PROGRAMS_ROOT) -> tuple[ActionItem, ...]:
    path = get_actions_path(program_id, programs_root)
    if not path.exists():
        return ()
    base_entries: dict[str, ActionItem] = {}
    for record in read_jsonl_records(path):
        record_type = record.get("record_type", "action")
        if record_type != "action":
            continue
        # Strict field-presence gate: every action row must at least carry id, program_id,
        # text, owner_alias, status, source_type, and created_at. The deeper parsers below
        # will enforce type correctness; this is the top-level presence assertion.
        validate_jsonl_row(
            record,
            required_fields=(
                "id",
                "program_id",
                "text",
                "owner_alias",
                "status",
                "source_type",
                "created_at",
            ),
            field_name="action row",
        )
        action = _action_from_record(record)
        if action.id not in base_entries:
            base_entries[action.id] = action
    latest_statuses = load_latest_action_statuses(program_id, programs_root)
    return tuple(
        _apply_status_update(entry, latest_statuses.get(entry.id))
        for entry in base_entries.values()
    )


def update_action_status(
    program_id: str,
    action_id: str,
    new_status: str | ActionStatus,
    note: str | None,
    *,
    updated_by: str,
    updated_at: datetime | None = None,
    programs_root: Path = PROGRAMS_ROOT,
) -> Path:
    current_actions = load_actions(program_id, programs_root)
    current_action = next((action for action in current_actions if action.id == action_id), None)
    if current_action is None:
        raise ValueError(f"Unknown action '{action_id}' for program '{program_id}'.")
    status_value = new_status if isinstance(new_status, ActionStatus) else ActionStatus.from_string(new_status)
    update = ActionStatusUpdate(
        action_id=action_id,
        new_status=status_value,
        updated_at=updated_at or datetime.now(timezone.utc),
        updated_by=updated_by,
        note=note,
    )
    _sync_action_fact(program_id, _apply_status_update(current_action, update), recorded_at=update.updated_at, programs_root=programs_root)
    target = get_actions_path(program_id, programs_root)
    payload = json.dumps(_status_update_to_record(update), separators=(",", ":")) + os.linesep
    append_jsonl_line(target, payload, max_bytes=_ACTIONS_MAX_BYTES)
    return target


def associate_action_with_work_item(
    program_id: str,
    action_id: str,
    work_item_id: int,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> None:
    path = get_actions_path(program_id, programs_root)
    if not path.exists():
        return
    
    records = read_jsonl_records(path)
    updated_records = []
    for record in records:
        if record.get("record_type") == "action" and record.get("id") == action_id:
            record["status"] = "open"
            linked = record.get("linked_work_item_ids") or []
            if work_item_id not in linked:
                linked.append(work_item_id)
            record["linked_work_item_ids"] = linked
        updated_records.append(record)
    
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        portalocker.lock(handle, portalocker.LockFlags.EXCLUSIVE)
        for record in updated_records:
            payload = json.dumps(record, separators=(",", ":")) + os.linesep
            handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
        portalocker.unlock(handle)
    write_checksum_file(path)

    updated_action = next((action for action in load_actions(program_id, programs_root) if action.id == action_id), None)
    if updated_action is not None:
        _sync_action_fact(
            program_id,
            updated_action,
            recorded_at=datetime.now(timezone.utc),
            programs_root=programs_root,
        )


def assess_action_staleness(actions: tuple[ActionItem, ...], as_of: date) -> tuple[ActionItem, ...]:
    return tuple(
        action
        for action in actions
        if action.due_date is not None and action.due_date < as_of and action.status in _ACTIVE_ACTION_STATUSES
    )


def can_promote_action(action: ActionItem) -> bool:
    """FR-SG-14: Return True when action meets all pre-promotion prerequisites.

    An action can be promoted from triage_only when:
    - owner_alias is a non-empty string
    - due_date is set
    - at least one ADO work item is linked
    Actions failing this gate remain in triage_only status.
    """
    return bool(action.owner_alias) and action.due_date is not None and bool(action.linked_work_item_ids)


def match_action_to_ado_update(action: ActionItem, trajectories: dict[int, tuple[TrajectoryPoint, ...]]) -> bool:
    created_date = action.created_at.date()
    for work_item_id in action.linked_work_item_ids:
        for point in trajectories.get(work_item_id, ()): 
            if point.date > created_date:
                return True
    return False


def load_action_resolution_candidate_ids(
    program_id: str,
    actions: tuple[ActionItem, ...],
    programs_root: Path = PROGRAMS_ROOT,
) -> frozenset[str]:
    reconcilable_actions = tuple(
        action
        for action in actions
        if action.status in _ACTIVE_ACTION_STATUSES and action.linked_work_item_ids
    )
    if not reconcilable_actions:
        return frozenset()

    trajectory_store = build_trajectory_store_for_program_id(program_id, programs_root=programs_root)
    trajectories = {
        work_item_id: trajectory_store.read(program_id, work_item_id)
        for action in reconcilable_actions
        for work_item_id in action.linked_work_item_ids
    }
    return frozenset(
        action.id
        for action in reconcilable_actions
        if match_action_to_ado_update(action, trajectories)
    )


def _apply_status_update(action: ActionItem, update: ActionStatusUpdate | None) -> ActionItem:
    if update is None:
        return action
    return replace(
        action,
        status=update.new_status,
        resolved_at=update.updated_at if update.new_status in _RESOLVED_ACTION_STATUSES else action.resolved_at,
        resolution_note=update.note if update.new_status in _RESOLVED_ACTION_STATUSES else action.resolution_note,
    )


def _sync_action_fact(
    program_id: str,
    action: ActionItem,
    *,
    recorded_at: datetime,
    programs_root: Path,
    correlation_id: str = "",
) -> None:
    from src.core.program_fact_store import FactPrecedence, ProgramFactInput, ProgramFactStore

    write_result = ProgramFactStore(program_id, db_root=_resolve_fact_db_root(programs_root)).append_fact(
        ProgramFactInput(
            fact_type="action.item",
            scope="program",
            entity_refs=(f"ACTION:{action.id}",),
            payload={
                "id": action.id,
                "program_id": action.program_id,
                "text": action.text,
                "owner_alias": action.owner_alias,
                "due_date": action.due_date.isoformat() if action.due_date is not None else None,
                "status": action.status.value,
                "source_signal_id": action.source_signal_id,
                "source_type": action.source_type.value,
                "linked_work_item_ids": list(action.linked_work_item_ids),
                "linked_claim_id": action.linked_claim_id,
                "linked_risk_id": action.linked_risk_id,
                "workstream_id": action.workstream_id,
                "created_at": action.created_at.isoformat(),
                "resolved_at": action.resolved_at.isoformat() if action.resolved_at is not None else None,
                "resolution_note": action.resolution_note,
            },
            source_signal_ids=(action.source_signal_id,) if action.source_signal_id else (),
            precedence=FactPrecedence.ACTIVE_PM_JUDGMENT,
            created_by="vertex.action_tracker",
        ),
        recorded_at=recorded_at,
    )
    # ADF-W2.12: skip the trace link on a genuine content-dedup no-op (e.g. a
    # retried append_action for an already-persisted action) -- only a real
    # write is worth recording.
    if correlation_id and write_result.action != "noop":
        try:
            record_trace_link(
                program_id=program_id,
                correlation_id=correlation_id,
                workflow_id=correlation_id,
                run_id=correlation_id,
                stage="fact",
                ref_type=REF_TYPE_FACT,
                ref_id=f"action.item:{action.id}@{recorded_at.isoformat()}",
                programs_root=programs_root,
            )
        except Exception:  # noqa: BLE001 -- a trace link is observability, never a write blocker.
            pass


def _resolve_fact_db_root(programs_root: Path) -> Path | None:
    if programs_root.name == "programs":
        return programs_root.parent
    return programs_root


def _action_to_record(action: ActionItem) -> dict[str, Any]:
    return {
        "record_type": "action",
        "id": action.id,
        "program_id": action.program_id,
        "text": action.text,
        "owner_alias": action.owner_alias,
        "due_date": action.due_date.isoformat() if action.due_date is not None else None,
        "status": action.status.value,
        "source_signal_id": action.source_signal_id,
        "source_type": action.source_type.value,
        "linked_work_item_ids": list(action.linked_work_item_ids),
        "linked_claim_id": action.linked_claim_id,
        "linked_risk_id": action.linked_risk_id,
        "workstream_id": action.workstream_id,
        "created_at": action.created_at.isoformat(),
        "resolved_at": action.resolved_at.isoformat() if action.resolved_at is not None else None,
        "resolution_note": action.resolution_note,
    }


def _action_from_record(record: dict[str, Any]) -> ActionItem:
    return ActionItem(
        id=_required_string(record["id"], field_name="id"),
        program_id=_required_string(record["program_id"], field_name="program_id"),
        text=_required_string(record["text"], field_name="text"),
        owner_alias=_required_string(record["owner_alias"], field_name="owner_alias"),
        due_date=_parse_optional_date(record.get("due_date")),
        status=ActionStatus.from_string(_required_string(record["status"], field_name="status")),
        source_signal_id=_parse_optional_string(record.get("source_signal_id"), field_name="source_signal_id"),
        source_type=ActionSourceType.from_string(_required_string(record["source_type"], field_name="source_type")),
        linked_work_item_ids=_parse_linked_work_item_ids(record.get("linked_work_item_ids")),
        linked_claim_id=_parse_optional_string(record.get("linked_claim_id"), field_name="linked_claim_id"),
        linked_risk_id=_parse_optional_string(record.get("linked_risk_id"), field_name="linked_risk_id"),
        workstream_id=_parse_optional_string(record.get("workstream_id"), field_name="workstream_id"),
        created_at=_parse_required_datetime(record["created_at"], field_name="created_at"),
        resolved_at=_parse_optional_datetime(record.get("resolved_at")),
        resolution_note=_parse_optional_string(record.get("resolution_note"), field_name="resolution_note"),
    )


def _status_update_to_record(update: ActionStatusUpdate) -> dict[str, Any]:
    return {
        "record_type": update.record_type,
        "action_id": update.action_id,
        "new_status": update.new_status.value,
        "updated_at": update.updated_at.isoformat(),
        "updated_by": update.updated_by,
        "note": update.note,
    }


def _status_update_from_record(record: dict[str, Any]) -> ActionStatusUpdate:
    return ActionStatusUpdate(
        action_id=_required_string(record["action_id"], field_name="action_id"),
        new_status=ActionStatus.from_string(_required_string(record["new_status"], field_name="new_status")),
        updated_at=_parse_required_datetime(record["updated_at"], field_name="updated_at"),
        updated_by=_required_string(record["updated_by"], field_name="updated_by"),
        note=_parse_optional_string(record.get("note"), field_name="note"),
    )


def _parse_optional_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise TypeError("date field must be a string when provided")
    return date.fromisoformat(value)


def _parse_optional_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise TypeError("datetime field must be a string when provided")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("datetime field must include timezone information")
    return parsed.astimezone(timezone.utc)


def _parse_required_datetime(value: Any, *, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include timezone information")
    return parsed.astimezone(timezone.utc)


def _parse_optional_string(value: Any, *, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    text = value.strip()
    return text or None


def _required_string(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    return value


def _parse_linked_work_item_ids(value: Any) -> tuple[int, ...]:
    if value in (None, ""):
        return ()
    if not isinstance(value, list):
        raise TypeError("linked_work_item_ids must be a list of integers")
    linked_ids: list[int] = []
    for entry in value:
        if isinstance(entry, bool) or not isinstance(entry, int):
            raise TypeError("linked_work_item_ids must contain integers only")
        linked_ids.append(entry)
    return tuple(linked_ids)