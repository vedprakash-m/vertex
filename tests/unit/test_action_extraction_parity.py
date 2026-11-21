from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date, datetime, timezone

import pytest

from tests.support.action_extraction_parity import (
    NormalizedAction,
    ParityFinding,
    check_action_parity,
    normalize_action_items,
    normalize_meeting_actions,
)
from src.core.meeting_action import MeetingAction
from src.core.models_v2 import ActionItem, ActionSourceType, ActionStatus

_TS = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)


def _meeting(commitment: str, owner: str | None, due: date | None) -> MeetingAction:
    return MeetingAction(
        id="m-1",
        program_id="acme",
        meeting_ref="m1",
        commitment=commitment,
        owner_alias=owner,
        due_date=due,
        linked_work_item_id=None,
        blocks=(),
        source_span=commitment,
        extraction_method="deterministic",
        status="staged",
        rejection_reason=None,
        proposed_at=_TS,
        decided_at=None,
    )


def _item(text: str, owner: str, due: date | None) -> ActionItem:
    return ActionItem(
        id="a-1",
        program_id="acme",
        text=text,
        owner_alias=owner,
        due_date=due,
        status=ActionStatus.PROPOSED,
        source_signal_id="sig-1",
        source_type=ActionSourceType.MEETING_TRANSCRIPT,
        linked_work_item_ids=(),
        linked_claim_id=None,
        linked_risk_id=None,
        workstream_id=None,
        created_at=_TS,
        resolved_at=None,
        resolution_note=None,
    )


def test_normalizers_collapse_whitespace_and_case() -> None:
    assert normalize_meeting_actions((_meeting("  Ship   Ramp packet ", "priya", date(2026, 12, 1)),)) == (
        NormalizedAction(key="ship ramp packet", owner_alias="priya", due_date=date(2026, 12, 1)),
    )
    assert normalize_action_items((_item("Ship ramp\tPacket", "unknown", None),)) == (
        NormalizedAction(key="ship ramp packet", owner_alias="unknown", due_date=None),
    )


def test_identical_inputs_produce_no_findings() -> None:
    a = (NormalizedAction("ship", "priya", date(2026, 12, 1)),)
    assert check_action_parity(a, a) == ()


def test_reports_missing_and_mismatches_sorted_by_kind_then_key() -> None:
    a = (
        NormalizedAction("zeta", "priya", None),
        NormalizedAction("ship", "priya", date(2026, 12, 1)),
        NormalizedAction("only a", None, None),
    )
    b = (
        NormalizedAction("ship", "alex", date(2026, 12, 2)),
        NormalizedAction("zeta", "priya", None),
        NormalizedAction("only b", "sam", None),
    )

    assert check_action_parity(a, b) == (
        ParityFinding(kind="due_mismatch", key="ship", a_value="2026-12-01", b_value="2026-12-02"),
        ParityFinding(kind="missing_in_a", key="only b", a_value=None, b_value="only b"),
        ParityFinding(kind="missing_in_b", key="only a", a_value="only a", b_value=None),
        ParityFinding(kind="owner_mismatch", key="ship", a_value="priya", b_value="alex"),
    )


def test_owner_values_are_compared_raw_including_unknown_sentinel() -> None:
    findings = check_action_parity(
        (NormalizedAction("ship", None, None),),
        (NormalizedAction("ship", "unknown", None),),
    )
    assert findings == (ParityFinding(kind="owner_mismatch", key="ship", a_value=None, b_value="unknown"),)


def test_duplicate_keys_compare_first_occurrence_only() -> None:
    a = (NormalizedAction("ship", "priya", None), NormalizedAction("ship", "alex", None))
    b = (NormalizedAction("ship", "priya", None),)
    assert check_action_parity(a, b) == ()


def test_checker_does_not_mutate_inputs_and_results_are_frozen() -> None:
    a = [NormalizedAction("ship", "priya", None)]
    b = [NormalizedAction("other", "priya", None)]
    findings = check_action_parity(tuple(a), tuple(b))

    assert a == [NormalizedAction("ship", "priya", None)]
    assert b == [NormalizedAction("other", "priya", None)]
    with pytest.raises(FrozenInstanceError):
        findings[0].kind = "x"  # type: ignore[misc]
