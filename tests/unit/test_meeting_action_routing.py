"""ADF-W3.5: unit tests for src/core/meeting_action_routing.py."""

from __future__ import annotations

from dataclasses import replace
import json
from datetime import date
from pathlib import Path

import pytest

from src.core.meeting_action import MeetingAction, approve_meeting_action, reject_meeting_action
from src.core.meeting_action_routing import MeetingActionRoutingError, route_meeting_action_to_ado_proposal


def _staged_action(*, action_id: str = "det-action-1") -> MeetingAction:
    return MeetingAction(
        id=action_id,
        program_id="xpf",
        meeting_ref="meeting-1",
        commitment="Ship the deployment doc",
        owner_alias="alex",
        due_date=date(2026, 8, 1),
        linked_work_item_id=1001,
        blocks=("WI:1002",),
        source_span="Action: Ship the deployment doc | owner=alex | due=2026-08-01 | wi=1001 | blocks=WI:1002",
        extraction_method="deterministic",
        status="staged",
    )


def test_staged_action_cannot_be_routed(tmp_path: Path) -> None:
    action = _staged_action()
    with pytest.raises(MeetingActionRoutingError, match="not 'approved'"):
        route_meeting_action_to_ado_proposal(
            action, org="msazure", project="One", programs_root=tmp_path / "programs"
        )


def test_rejected_action_cannot_be_routed(tmp_path: Path) -> None:
    action = reject_meeting_action(_staged_action(), reason="duplicate of an existing task")
    with pytest.raises(MeetingActionRoutingError):
        route_meeting_action_to_ado_proposal(
            action, org="msazure", project="One", programs_root=tmp_path / "programs"
        )


def test_approved_action_is_routed_with_correct_payload(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    action = approve_meeting_action(_staged_action(), approved_by="pm@example.com")

    entry = route_meeting_action_to_ado_proposal(
        action, org="msazure", project="One", area_path="One\\XPF", iteration_path="Sprint 5",
        programs_root=programs_root,
    )

    payload = json.loads(entry.payload_json)
    assert payload["title"] == "Ship the deployment doc"
    assert payload["assigned_to"] == "alex"
    assert payload["area_path"] == "One\\XPF"
    assert payload["iteration_path"] == "Sprint 5"
    assert "2026-08-01" in payload["description"]
    assert "WI:1002" in payload["description"]
    assert action.source_span in payload["description"]


def test_routing_the_same_approved_action_twice_is_duplicate_safe(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    action = approve_meeting_action(_staged_action(), approved_by="pm@example.com")

    first = route_meeting_action_to_ado_proposal(action, org="msazure", project="One", programs_root=programs_root)
    second = route_meeting_action_to_ado_proposal(action, org="msazure", project="One", programs_root=programs_root)

    assert first.outbox_id == second.outbox_id
    assert first.correlation_id == second.correlation_id


def test_different_actions_get_different_outbox_entries(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    action_a = approve_meeting_action(_staged_action(action_id="det-action-1"), approved_by="pm@example.com")
    action_b = approve_meeting_action(_staged_action(action_id="det-action-2"), approved_by="pm@example.com")

    entry_a = route_meeting_action_to_ado_proposal(action_a, org="msazure", project="One", programs_root=programs_root)
    entry_b = route_meeting_action_to_ado_proposal(action_b, org="msazure", project="One", programs_root=programs_root)

    assert entry_a.outbox_id != entry_b.outbox_id


def test_title_is_truncated_when_commitment_is_very_long(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    action = approve_meeting_action(
        replace(_staged_action(), commitment="x" * 400), approved_by="pm@example.com"
    )
    entry = route_meeting_action_to_ado_proposal(action, org="msazure", project="One", programs_root=programs_root)
    payload = json.loads(entry.payload_json)
    assert len(payload["title"]) <= 255


def test_payload_omits_absent_optional_fields(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    minimal = MeetingAction(
        id="det-action-minimal", program_id="xpf", meeting_ref="meeting-1", commitment="Follow up",
        owner_alias=None, due_date=None, linked_work_item_id=None, blocks=(), source_span="Action: Follow up",
        extraction_method="deterministic", status="approved",
    )
    entry = route_meeting_action_to_ado_proposal(minimal, org="msazure", project="One", programs_root=programs_root)
    payload = json.loads(entry.payload_json)
    assert "assigned_to" not in payload
    assert "area_path" not in payload
    assert "iteration_path" not in payload
