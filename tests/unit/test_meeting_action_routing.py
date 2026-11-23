"""ADF-W3.5: unit tests for src/core/meeting_action_routing.py."""

from __future__ import annotations

from dataclasses import replace
import json
from datetime import date
from pathlib import Path

import pytest
import src.core.meeting_action_routing as meeting_action_routing

from src.core.meeting_action import MeetingAction, approve_meeting_action, reject_meeting_action
from src.core.actuation_outbox import dispatch_leased_create_task
from src.core.meeting_action_routing import MeetingActionRoutingError, route_meeting_action_to_ado_proposal
from src.core._db import open_program_db_with_retry
from src.core.ledger.durable_outbox_store import OutboxError, load_effect_outbox_id
from src.core.actuation_outbox import outbox_db_path
from src.core.operation_capabilities import (
    create_target_identity,
    normalize_semantic_payload,
    semantic_effect_key,
)
from src.m365.ado_writer import _authorized_create_effect


def _staged_action(*, action_id: str = "det-action-1") -> MeetingAction:
    return MeetingAction(
        id=action_id,
        program_id="samplealpha",
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
            action, org="contoso", project="One", programs_root=tmp_path / "programs"
        )


@pytest.mark.parametrize("approval_field", ["approved_by", "decided_at"])
def test_approved_action_requires_retained_approval_metadata(
    tmp_path: Path, approval_field: str,
) -> None:
    action = approve_meeting_action(
        _staged_action(), approved_by="pm@example.com"
    )
    action = replace(action, **{approval_field: None})

    with pytest.raises(MeetingActionRoutingError, match="retained approval identity"):
        route_meeting_action_to_ado_proposal(
            action, org="contoso", project="One",
            programs_root=tmp_path / "programs",
        )


def test_rejected_action_cannot_be_routed(tmp_path: Path) -> None:
    action = reject_meeting_action(_staged_action(), reason="duplicate of an existing task")
    with pytest.raises(MeetingActionRoutingError):
        route_meeting_action_to_ado_proposal(
            action, org="contoso", project="One", programs_root=tmp_path / "programs"
        )


def test_meeting_action_approval_refuses_blank_reviewer() -> None:
    with pytest.raises(ValueError, match="reviewer identity"):
        approve_meeting_action(_staged_action(), approved_by="  ")


def test_approved_action_is_routed_with_correct_payload(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    action = approve_meeting_action(_staged_action(), approved_by="pm@example.com")

    entry = route_meeting_action_to_ado_proposal(
        action, org="contoso", project="One", area_path="One\\SAMPLEALPHA", iteration_path="Sprint 5",
        programs_root=programs_root,
    )

    payload = json.loads(entry.payload_json)
    assert payload["title"] == "Ship the deployment doc"
    assert payload["assigned_to"] == "alex"
    assert payload["area_path"] == "One\\SAMPLEALPHA"
    assert payload["iteration_path"] == "Sprint 5"
    assert "2026-08-01" in payload["description"]
    assert "WI:1002" in payload["description"]
    assert action.source_span in payload["description"]


def test_routing_the_same_approved_action_twice_is_duplicate_safe(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    action = approve_meeting_action(_staged_action(), approved_by="pm@example.com")

    first = route_meeting_action_to_ado_proposal(action, org="contoso", project="One", programs_root=programs_root)
    second = route_meeting_action_to_ado_proposal(action, org="contoso", project="One", programs_root=programs_root)

    assert first.outbox_id == second.outbox_id
    assert first.correlation_id == second.correlation_id


def test_routing_binds_the_exact_semantic_effect_identity(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    action = approve_meeting_action(
        _staged_action(), approved_by="pm@example.com"
    )

    entry = route_meeting_action_to_ado_proposal(
        action,
        org="contoso",
        project="One",
        area_path="One\\SAMPLEALPHA",
        programs_root=programs_root,
    )

    payload = json.loads(entry.payload_json)
    desired = normalize_semantic_payload("create_task.v1", payload)
    causal_occurrence_id = f"meeting-action:{action.id}"
    effect_id = semantic_effect_key(
        program_id=action.program_id,
        provider="ado",
        tenant="contoso",
        project="One",
        operation="create_task.v1",
        target_identity=create_target_identity(
            provider="ado",
            tenant="contoso",
            project="One",
            causal_occurrence_id=causal_occurrence_id,
            payload=desired,
        ),
        causal_occurrence_id=causal_occurrence_id,
        payload=desired,
    )
    db_path = programs_root / action.program_id / "runtime" / "actuation" / "outbox.db"
    assert load_effect_outbox_id(db_path, effect_id) == entry.outbox_id


def test_routed_action_reaches_dispatch_provider_after_authorization_preflight(
    tmp_path: Path,
) -> None:
    programs_root = tmp_path / "programs"
    action = approve_meeting_action(
        _staged_action(), approved_by="pm@example.com"
    )
    entry = route_meeting_action_to_ado_proposal(
        action, org="contoso", project="One", programs_root=programs_root
    )
    provider_calls: list[str] = []

    def lost_response(current) -> None:
        provider_calls.append(current.outbox_id)
        raise TimeoutError("provider response was lost")

    outcome = dispatch_leased_create_task(
        program_id=action.program_id,
        idempotency_key=entry.outbox_id,
        owner="worker",
        dispatch_fn=lost_response,
        programs_root=programs_root,
    )

    assert provider_calls == [entry.outbox_id]
    assert outcome.status == "uncertain_remote_state"


def test_routed_action_binds_real_ado_authorization_path(tmp_path: Path) -> None:
    action = approve_meeting_action(
        _staged_action(), approved_by="pm@example.com"
    )
    entry = route_meeting_action_to_ado_proposal(
        action,
        org="contoso",
        project="One",
        programs_root=tmp_path / "programs",
    )

    authorized = _authorized_create_effect(json.loads(entry.payload_json))

    assert authorized.execution_principal == "pm@example.com"
    assert authorized.target_identity.startswith("origin1:")


def test_routed_action_refuses_tampered_authorized_destination(
    tmp_path: Path,
) -> None:
    programs_root = tmp_path / "programs"
    action = approve_meeting_action(
        _staged_action(), approved_by="pm@example.com"
    )
    entry = route_meeting_action_to_ado_proposal(
        action, org="contoso", project="One", programs_root=programs_root
    )
    payload = json.loads(entry.payload_json)
    payload["authorized_effect"]["project"] = "Foreign"
    db_path = outbox_db_path(action.program_id, programs_root=programs_root)
    with open_program_db_with_retry(db_path, durability="strict") as connection:
        connection.execute(
            "UPDATE outbox SET payload_json = ? WHERE outbox_id = ?",
            (json.dumps(payload, sort_keys=True), entry.outbox_id),
        )
    provider_calls: list[str] = []

    outcome = dispatch_leased_create_task(
        program_id=action.program_id,
        idempotency_key=entry.outbox_id,
        owner="worker",
        dispatch_fn=lambda current: provider_calls.append(current.outbox_id),
        programs_root=programs_root,
    )

    assert provider_calls == []
    assert outcome.status == "failed_terminal"
    assert "does not match the outbox row" in (outcome.entry.failure_reason or "")


@pytest.mark.parametrize("tamper", ["missing", "mismatched"])
def test_routed_action_refuses_tampered_semantic_effect(
    tmp_path: Path, tamper: str,
) -> None:
    programs_root = tmp_path / "programs"
    action = approve_meeting_action(
        _staged_action(), approved_by="pm@example.com"
    )
    entry = route_meeting_action_to_ado_proposal(
        action, org="contoso", project="One", programs_root=programs_root
    )
    payload = json.loads(entry.payload_json)
    if tamper == "missing":
        payload.pop("semantic_effect_id")
    else:
        payload["semantic_effect_id"] = "sek1:rebound"
    db_path = outbox_db_path(action.program_id, programs_root=programs_root)
    with open_program_db_with_retry(db_path, durability="strict") as connection:
        connection.execute(
            "UPDATE outbox SET payload_json = ? WHERE outbox_id = ?",
            (json.dumps(payload, sort_keys=True), entry.outbox_id),
        )

    with pytest.raises(OutboxError, match="missing, changed or unbound"):
        dispatch_leased_create_task(
            program_id=action.program_id,
            idempotency_key=entry.outbox_id,
            owner="worker",
            dispatch_fn=lambda current: pytest.fail("provider must not be reached"),
            programs_root=programs_root,
        )


def test_routing_refuses_when_operation_has_no_remote_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    action = approve_meeting_action(
        _staged_action(), approved_by="pm@example.com"
    )
    monkeypatch.setattr(
        meeting_action_routing, "approved_remote_marker", lambda **_kwargs: None
    )

    with pytest.raises(MeetingActionRoutingError, match="no approved remote marker"):
        route_meeting_action_to_ado_proposal(
            action, org="contoso", project="One",
            programs_root=tmp_path / "programs",
        )


def test_different_actions_get_different_outbox_entries(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    action_a = approve_meeting_action(_staged_action(action_id="det-action-1"), approved_by="pm@example.com")
    action_b = approve_meeting_action(_staged_action(action_id="det-action-2"), approved_by="pm@example.com")

    entry_a = route_meeting_action_to_ado_proposal(action_a, org="contoso", project="One", programs_root=programs_root)
    entry_b = route_meeting_action_to_ado_proposal(action_b, org="contoso", project="One", programs_root=programs_root)

    assert entry_a.outbox_id != entry_b.outbox_id


def test_title_is_truncated_when_commitment_is_very_long(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    action = approve_meeting_action(
        replace(_staged_action(), commitment="x" * 400), approved_by="pm@example.com"
    )
    entry = route_meeting_action_to_ado_proposal(action, org="contoso", project="One", programs_root=programs_root)
    payload = json.loads(entry.payload_json)
    assert len(payload["title"]) <= 255


def test_payload_omits_absent_optional_fields(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    minimal = approve_meeting_action(MeetingAction(
        id="det-action-minimal", program_id="samplealpha", meeting_ref="meeting-1", commitment="Follow up",
        owner_alias=None, due_date=None, linked_work_item_id=None, blocks=(), source_span="Action: Follow up",
        extraction_method="deterministic",
    ), approved_by="pm@example.com")
    entry = route_meeting_action_to_ado_proposal(minimal, org="contoso", project="One", programs_root=programs_root)
    payload = json.loads(entry.payload_json)
    assert "assigned_to" not in payload
    assert "area_path" not in payload
    assert "iteration_path" not in payload
