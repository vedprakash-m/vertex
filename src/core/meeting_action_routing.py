"""ADF-W3.5 (specs/arch-data-fix.md Section 11.4): "Route reviewed meeting
actions through outbox-backed ADO proposals."

Reuses ADF-W1.3's `actuation_outbox.py` create-task machinery exactly --
this module builds the `payload_json` a `MeetingAction` maps to and derives
a stable `operation_intent_id`/idempotency key from the action's own id, then
calls the same `enqueue_create_task_intent` every other create-task caller
uses. It does not duplicate dispatch, leasing, or ADO-patch construction:
`ADOWriter._dispatch_create_task_outbox_entry`/`_build_create_task_patch`
already handle any outbox row in this domain regardless of what produced
it, so a routed `MeetingAction` is dispatched by the exact same, already-
tested path as any other create-task proposal.
"""

from __future__ import annotations

import json
from pathlib import Path

from src.core.actuation_outbox import create_task_idempotency_key, enqueue_create_task_intent
from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.ledger.durable_outbox_store import OutboxEntry
from src.core.meeting_action import MeetingAction

_MAX_TITLE_LENGTH = 255


class MeetingActionRoutingError(Exception):
    """Raised when a MeetingAction is not eligible for outbox routing."""


def route_meeting_action_to_ado_proposal(
    action: MeetingAction,
    *,
    org: str,
    project: str,
    area_path: str | None = None,
    iteration_path: str | None = None,
    programs_root: Path = PROGRAMS_ROOT,
) -> OutboxEntry:
    """Enqueues a reviewed (``status == "approved"``) MeetingAction as a
    create-task outbox intent. Idempotent by construction: the
    ``operation_intent_id`` is derived deterministically from
    ``action.id`` (not a fresh uuid per call), so routing the same
    approved action twice -- the "duplicate-safe meeting-to-ADO fixture"
    acceptance evidence -- reuses the already-enqueued row via
    ``enqueue_create_task_intent``'s existing idempotency (ADF-W1.3),
    never creates a second outbox entry."""
    if action.status != "approved":
        raise MeetingActionRoutingError(
            f"MeetingAction {action.id!r} has status {action.status!r}, not 'approved' -- "
            "only a reviewed and approved action may be routed to an ADO proposal (Section 11.4's "
            "golden workflow requires reviewed actions before ADO proposal)."
        )

    operation_intent_id = f"meeting-action:{action.id}"
    idempotency_key = create_task_idempotency_key(
        program_id=action.program_id,
        org=org,
        project=project,
        operation_intent_id=operation_intent_id,
    )
    payload = _build_payload(action, area_path=area_path, iteration_path=iteration_path)
    return enqueue_create_task_intent(
        program_id=action.program_id,
        idempotency_key=idempotency_key,
        operation_intent_id=operation_intent_id,
        proposal_id=action.id,
        payload_json=json.dumps(payload, sort_keys=True),
        programs_root=programs_root,
    )


def _build_payload(
    action: MeetingAction, *, area_path: str | None, iteration_path: str | None
) -> dict[str, object]:
    title = action.commitment.strip()
    if len(title) > _MAX_TITLE_LENGTH:
        title = title[: _MAX_TITLE_LENGTH - 1].rstrip() + "…"

    description_lines = [action.commitment.strip(), ""]
    description_lines.append(f"Source meeting: {action.meeting_ref}")
    description_lines.append(f"Extraction method: {action.extraction_method}")
    if action.due_date is not None:
        description_lines.append(f"Committed due date: {action.due_date.isoformat()}")
    if action.blocks:
        description_lines.append(f"Blocks: {', '.join(action.blocks)}")
    description_lines.append("")
    description_lines.append(f"Source span: {action.source_span}")

    payload: dict[str, object] = {
        "title": title,
        "description": "\n".join(description_lines),
    }
    if action.owner_alias:
        payload["assigned_to"] = action.owner_alias
    if area_path:
        payload["area_path"] = area_path
    if iteration_path:
        payload["iteration_path"] = iteration_path
    return payload


__all__ = ["MeetingActionRoutingError", "route_meeting_action_to_ado_proposal"]
