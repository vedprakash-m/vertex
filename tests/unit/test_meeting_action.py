"""ADF-W3.3/ADF-W3.4: unit tests for src/core/meeting_action.py."""

from __future__ import annotations

from datetime import date

import pytest

from src.core.meeting_action import (
    MeetingAction,
    approve_meeting_action,
    extract_deterministic_meeting_actions,
    merge_meeting_actions,
    reject_meeting_action,
    validate_meeting_actions,
)
from src.core.models import RiskLevel, WorkItem


def _work_item(item_id: int, *, state: str = "Active") -> WorkItem:
    return WorkItem(
        id=item_id,
        type="Task",
        title=f"Item {item_id}",
        state=state,
        assigned_to="Priya",
        assigned_to_email="priya@example.com",
        area_path="One\\Demo",
        iteration_path="Sprint 1",
        target_date=None,
        risk_level=RiskLevel.LOW,
        tags=[],
        custom_fields={},
    )


# ---------------------------------------------------------------------------
# Deterministic extraction
# ---------------------------------------------------------------------------


def test_extracts_a_full_marker_line() -> None:
    text = "Action: Ship the deployment doc | owner=alex | due=2026-08-01 | wi=1001 | blocks=WI:1002"
    actions = extract_deterministic_meeting_actions(program_id="xpf", meeting_ref="meeting-1", transcript_text=text)
    assert len(actions) == 1
    action = actions[0]
    assert action.commitment == "Ship the deployment doc"
    assert action.owner_alias == "alex"
    assert action.due_date == date(2026, 8, 1)
    assert action.linked_work_item_id == 1001
    assert action.blocks == ("WI:1002",)
    assert action.source_span == text
    assert action.extraction_method == "deterministic"
    assert action.status == "staged"


def test_extracts_minimal_marker_line_with_no_optional_fields() -> None:
    text = "Action: Follow up with legal"
    actions = extract_deterministic_meeting_actions(program_id="xpf", meeting_ref="meeting-1", transcript_text=text)
    assert len(actions) == 1
    assert actions[0].owner_alias is None
    assert actions[0].due_date is None
    assert actions[0].linked_work_item_id is None
    assert actions[0].blocks == ()


def test_ignores_non_marker_lines() -> None:
    text = "This is just ordinary meeting notes, not a marker line.\nAnother normal sentence."
    actions = extract_deterministic_meeting_actions(program_id="xpf", meeting_ref="meeting-1", transcript_text=text)
    assert actions == ()


def test_skips_malformed_marker_line_with_unknown_key() -> None:
    text = "Action: Do the thing | notakey=value"
    actions = extract_deterministic_meeting_actions(program_id="xpf", meeting_ref="meeting-1", transcript_text=text)
    assert actions == ()


def test_skips_marker_with_invalid_due_date() -> None:
    text = "Action: Do the thing | due=not-a-date"
    actions = extract_deterministic_meeting_actions(program_id="xpf", meeting_ref="meeting-1", transcript_text=text)
    assert actions == ()


def test_skips_marker_with_invalid_work_item_id() -> None:
    text = "Action: Do the thing | wi=not-a-number"
    actions = extract_deterministic_meeting_actions(program_id="xpf", meeting_ref="meeting-1", transcript_text=text)
    assert actions == ()


def test_owner_alias_normalizes_email_to_bare_alias() -> None:
    text = "Action: Do the thing | owner=Alex@Example.com"
    actions = extract_deterministic_meeting_actions(program_id="xpf", meeting_ref="meeting-1", transcript_text=text)
    assert actions[0].owner_alias == "alex"


def test_multiple_marker_lines_get_sequential_ids() -> None:
    text = "Action: First thing\nAction: Second thing"
    actions = extract_deterministic_meeting_actions(program_id="xpf", meeting_ref="meeting-1", transcript_text=text)
    assert len(actions) == 2
    assert actions[0].id != actions[1].id


# ---------------------------------------------------------------------------
# Merge and deduplicate
# ---------------------------------------------------------------------------


def _action(*, method: str, commitment: str = "Ship the doc", owner: str | None = "alex", suffix: str = "1") -> MeetingAction:
    return MeetingAction(
        id=f"{method}-action-{suffix}",
        program_id="xpf",
        meeting_ref="meeting-1",
        commitment=commitment,
        owner_alias=owner,
        due_date=None,
        linked_work_item_id=None,
        blocks=(),
        source_span=commitment,
        extraction_method=method,  # type: ignore[arg-type]
    )


def test_merge_keeps_all_when_no_overlap() -> None:
    deterministic = (_action(method="deterministic", commitment="Ship the doc"),)
    llm = (_action(method="llm", commitment="Follow up with legal", suffix="2"),)
    result = merge_meeting_actions(deterministic, llm)
    assert len(result.actions) == 2
    assert result.warnings == ()


def test_merge_drops_llm_duplicate_of_deterministic() -> None:
    deterministic = (_action(method="deterministic", commitment="Ship the doc", owner="alex"),)
    llm = (_action(method="llm", commitment="  Ship   the doc  ", owner="Alex", suffix="2"),)
    result = merge_meeting_actions(deterministic, llm)
    assert len(result.actions) == 1
    assert result.actions[0].extraction_method == "deterministic"
    assert len(result.warnings) == 1


def test_merge_treats_different_owners_as_distinct() -> None:
    deterministic = (_action(method="deterministic", commitment="Ship the doc", owner="alex"),)
    llm = (_action(method="llm", commitment="Ship the doc", owner="priya", suffix="2"),)
    result = merge_meeting_actions(deterministic, llm)
    assert len(result.actions) == 2


# ---------------------------------------------------------------------------
# Validation (ADF-W3.4)
# ---------------------------------------------------------------------------


def test_valid_action_stays_staged() -> None:
    transcript = "Action: Ship the doc | wi=1001"
    action = extract_deterministic_meeting_actions(program_id="xpf", meeting_ref="m1", transcript_text=transcript)[0]
    validated = validate_meeting_actions((action,), transcript_text=transcript, items=(_work_item(1001),))
    assert validated[0].status == "staged"
    assert validated[0].rejection_reason is None


def test_action_with_source_span_not_in_transcript_is_rejected() -> None:
    fabricated = MeetingAction(
        id="a1", program_id="xpf", meeting_ref="m1", commitment="Do X", owner_alias=None,
        due_date=None, linked_work_item_id=None, blocks=(), source_span="this text is not in the transcript",
        extraction_method="llm",
    )
    validated = validate_meeting_actions((fabricated,), transcript_text="completely different transcript content", items=())
    assert validated[0].status == "rejected"
    assert "fabrication" in validated[0].rejection_reason


def test_action_with_empty_source_span_is_rejected() -> None:
    action = MeetingAction(
        id="a1", program_id="xpf", meeting_ref="m1", commitment="Do X", owner_alias=None,
        due_date=None, linked_work_item_id=None, blocks=(), source_span="   ",
        extraction_method="llm",
    )
    validated = validate_meeting_actions((action,), transcript_text="anything", items=())
    assert validated[0].status == "rejected"
    assert "empty" in validated[0].rejection_reason


def test_action_linked_to_work_item_outside_allowed_set_is_rejected() -> None:
    transcript = "Action: Do X | wi=9999"
    action = extract_deterministic_meeting_actions(program_id="xpf", meeting_ref="m1", transcript_text=transcript)[0]
    validated = validate_meeting_actions((action,), transcript_text=transcript, items=(_work_item(1001),))
    assert validated[0].status == "rejected"
    assert "not in the allowed work item set" in validated[0].rejection_reason


def test_action_linked_to_terminal_work_item_is_rejected() -> None:
    transcript = "Action: Do X | wi=1001"
    action = extract_deterministic_meeting_actions(program_id="xpf", meeting_ref="m1", transcript_text=transcript)[0]
    validated = validate_meeting_actions((action,), transcript_text=transcript, items=(_work_item(1001, state="Closed"),))
    assert validated[0].status == "rejected"
    assert "terminal state" in validated[0].rejection_reason


def test_action_with_blocks_ref_outside_allowed_set_is_rejected() -> None:
    transcript = "Action: Do X | blocks=WI:9999"
    action = extract_deterministic_meeting_actions(program_id="xpf", meeting_ref="m1", transcript_text=transcript)[0]
    validated = validate_meeting_actions((action,), transcript_text=transcript, items=(_work_item(1001),))
    assert validated[0].status == "rejected"
    assert "blocks entry WI:9999" in validated[0].rejection_reason


def test_action_with_valid_blocks_ref_stays_staged() -> None:
    transcript = "Action: Do X | blocks=WI:1001"
    action = extract_deterministic_meeting_actions(program_id="xpf", meeting_ref="m1", transcript_text=transcript)[0]
    validated = validate_meeting_actions((action,), transcript_text=transcript, items=(_work_item(1001),))
    assert validated[0].status == "staged"


def test_multiple_findings_are_joined_in_rejection_reason() -> None:
    action = MeetingAction(
        id="a1", program_id="xpf", meeting_ref="m1", commitment="", owner_alias=None,
        due_date=None, linked_work_item_id=None, blocks=(), source_span="",
        extraction_method="llm",
    )
    validated = validate_meeting_actions((action,), transcript_text="anything", items=())
    assert validated[0].status == "rejected"
    assert "empty" in validated[0].rejection_reason
    assert "commitment text is empty" in validated[0].rejection_reason


# ---------------------------------------------------------------------------
# Human review lifecycle (ADF-W3.5 precondition)
# ---------------------------------------------------------------------------


def test_approve_staged_action_sets_status_approved() -> None:
    text = "Action: Ship the doc"
    action = extract_deterministic_meeting_actions(program_id="xpf", meeting_ref="m1", transcript_text=text)[0]
    approved = approve_meeting_action(action, approved_by="pm@example.com")
    assert approved.status == "approved"


def test_approve_rejected_action_raises() -> None:
    action = MeetingAction(
        id="a1", program_id="xpf", meeting_ref="m1", commitment="", owner_alias=None, due_date=None,
        linked_work_item_id=None, blocks=(), source_span="", extraction_method="llm", status="rejected",
        rejection_reason="empty commitment",
    )
    with pytest.raises(ValueError, match="rejected"):
        approve_meeting_action(action, approved_by="pm@example.com")


def test_reject_staged_action_records_reason() -> None:
    text = "Action: Ship the doc"
    action = extract_deterministic_meeting_actions(program_id="xpf", meeting_ref="m1", transcript_text=text)[0]
    rejected = reject_meeting_action(action, reason="duplicate of an existing task")
    assert rejected.status == "rejected"
    assert rejected.rejection_reason == "duplicate of an existing task"
