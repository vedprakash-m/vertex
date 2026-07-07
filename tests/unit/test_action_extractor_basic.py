from __future__ import annotations

from datetime import datetime, timezone

from src.core.action_extractor_basic import extract_actions_from_signals
from src.core.models import Confidence
from src.core.models_v2 import ActionStatus, Signal, SignalClass


def test_extract_actions_from_signals_builds_proposed_action_with_due_date_and_refs() -> None:
    signal = Signal(
        id="signal-1",
        timestamp=datetime(2026, 5, 10, 9, 0, tzinfo=timezone.utc),
        source="workiq/email",
        program_id="acme",
        workstream_id="deployment",
        entity_refs=("WI:1001",),
        text="Action: follow up with priya by 2026-05-14 on WI:1001 to confirm the ramp packet.",
        raw_ref=None,
        confidence=Confidence.MEDIUM,
        metadata=None,
        thread_id=None,
    )

    actions = extract_actions_from_signals((signal,), program_id="acme")

    assert len(actions) == 1
    assert actions[0].status is ActionStatus.PROPOSED
    assert actions[0].owner_alias == "priya"
    assert actions[0].due_date is not None and actions[0].due_date.isoformat() == "2026-05-14"
    assert actions[0].linked_work_item_ids == (1001,)
    assert actions[0].source_signal_id == "signal-1"


def test_extract_actions_from_signals_ignores_non_action_text() -> None:
    signal = Signal(
        id="signal-2",
        timestamp=datetime(2026, 5, 10, 9, 0, tzinfo=timezone.utc),
        source="ado/revision",
        program_id="acme",
        workstream_id="deployment",
        entity_refs=("WI:1001",),
        text="WI:1001 target date moved to 2026-05-14.",
        raw_ref=None,
        confidence=Confidence.HIGH,
        metadata=None,
        thread_id=None,
    )

    assert extract_actions_from_signals((signal,), program_id="acme") == ()


def test_extract_actions_from_signals_falls_back_to_sender_alias_and_dedupes_semantic_duplicates() -> None:
    first = Signal(
        id="signal-3",
        timestamp=datetime(2026, 5, 10, 9, 0, tzinfo=timezone.utc),
        source="workiq/email",
        program_id="acme",
        workstream_id="deployment",
        entity_refs=(),
        text="Need to confirm the checkpoint by May 20.",
        raw_ref=None,
        confidence=Confidence.MEDIUM,
        metadata={"sender_alias": "owner"},
        thread_id=None,
    )
    second = Signal(
        id="signal-4",
        timestamp=datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc),
        source="workiq/email",
        program_id="acme",
        workstream_id="deployment",
        entity_refs=(),
        text="Need to confirm the checkpoint by May 20.",
        raw_ref=None,
        confidence=Confidence.MEDIUM,
        metadata={"sender_alias": "owner"},
        thread_id=None,
    )

    actions = extract_actions_from_signals((first, second), program_id="acme")

    assert len(actions) == 1
    assert actions[0].owner_alias == "owner"
    assert actions[0].due_date is not None and actions[0].due_date.isoformat() == "2026-05-20"


def test_extract_actions_from_signals_supports_hyphenated_follow_up_phrasing() -> None:
    signal = Signal(
        id="signal-follow-up",
        timestamp=datetime(2026, 5, 10, 9, 0, tzinfo=timezone.utc),
        source="workiq/transcript",
        program_id="acme",
        workstream_id="deployment",
        entity_refs=("WI:1001",),
        text="Follow-up with Priya by May 20 on WI:1001 to confirm the checkpoint.",
        raw_ref=None,
        confidence=Confidence.MEDIUM,
        metadata=None,
        thread_id=None,
    )

    actions = extract_actions_from_signals((signal,), program_id="acme")

    assert len(actions) == 1
    assert actions[0].owner_alias == "priya"
    assert actions[0].due_date is not None and actions[0].due_date.isoformat() == "2026-05-20"
    assert actions[0].linked_work_item_ids == (1001,)


def test_extract_actions_from_signals_prefers_mitigation_signal_over_status_duplicate() -> None:
    status_signal = Signal(
        id="signal-status",
        timestamp=datetime(2026, 5, 10, 9, 0, tzinfo=timezone.utc),
        source="workiq/email",
        program_id="acme",
        workstream_id="deployment",
        entity_refs=("WI:1001",),
        text="Action: follow up with priya by 2026-05-14 on WI:1001 to confirm the ramp packet.",
        raw_ref=None,
        confidence=Confidence.MEDIUM,
        metadata={"signal_class": SignalClass.STATUS.value},
        thread_id=None,
    )
    mitigation_signal = Signal(
        id="signal-mitigation",
        timestamp=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        source="workiq/transcript",
        program_id="acme",
        workstream_id="deployment",
        entity_refs=("WI:1001",),
        text="Action: follow up with priya by 2026-05-14 on WI:1001 to confirm the ramp packet.",
        raw_ref=None,
        confidence=Confidence.MEDIUM,
        metadata={"signal_class": SignalClass.MITIGATION.value},
        thread_id=None,
    )

    actions = extract_actions_from_signals((status_signal, mitigation_signal), program_id="acme")

    assert len(actions) == 1
    assert actions[0].source_signal_id == "signal-mitigation"