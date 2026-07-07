"""Tests for signal_ref_utils — FR-SG-08 workstream WI-ref widening slice."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.core.models import Confidence
from src.core.models_v2 import (
    EmailThreadSource,
    Signal,
    TeamsMeetingSeries,
    TeamsChat,
    Workstream,
    WorkstreamSignalSources,
)
from src.core.signal_ref_utils import extract_work_item_refs, merge_entity_refs, widen_ws_wi_refs


def _signal(
    workstream_id: str | None = "ws-a",
    entity_refs: tuple[str, ...] = (),
) -> Signal:
    return Signal(
        id="sig-1",
        timestamp=datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc),
        source="workiq/teams",
        program_id="test",
        workstream_id=workstream_id,
        entity_refs=entity_refs,
        text="Some collaboration message",
        raw_ref=None,
        confidence=Confidence.MEDIUM,
    )


def _workstream(
    ws_id: str = "ws-a",
    meeting_wi: tuple[int, ...] = (),
    chat_wi: tuple[int, ...] = (),
    email_wi: tuple[int, ...] = (),
) -> Workstream:
    sources = WorkstreamSignalSources(
        teams_meeting_series=(
            TeamsMeetingSeries(display_name="standup", work_item_ids=meeting_wi),
        )
        if meeting_wi
        else (),
        teams_chats=(
            TeamsChat(display_name="chat", work_item_ids=chat_wi),
        )
        if chat_wi
        else (),
        email_threads=(
            EmailThreadSource(display_name="thread", thread_id="t1", work_item_ids=email_wi),
        )
        if email_wi
        else (),
    )
    return Workstream(id=ws_id, name="WS A", signal_sources=sources)


# --- extract_work_item_refs ---


def test_extract_work_item_refs_standard_wi_colon() -> None:
    assert extract_work_item_refs("See WI:12345 for details") == ("WI:12345",)


def test_extract_work_item_refs_ado_hash() -> None:
    assert extract_work_item_refs("Fixed in ADO#99001") == ("WI:99001",)


def test_extract_work_item_refs_url() -> None:
    result = extract_work_item_refs(
        "https://dev.azure.com/org/proj/_workitems/edit/54321"
    )
    assert result == ("WI:54321",)


def test_extract_work_item_refs_no_match() -> None:
    assert extract_work_item_refs("nothing here") == ()


# --- merge_entity_refs ---


def test_merge_entity_refs_adds_workstream_ref() -> None:
    result = merge_entity_refs(
        provider_refs=("WI:1001",), workstream_id="ws-a"
    )
    assert "WS:ws-a" in result
    assert "WI:1001" in result


def test_merge_entity_refs_no_workstream_id() -> None:
    result = merge_entity_refs(provider_refs=("WI:2000",), workstream_id=None)
    assert result == ("WI:2000",)
    assert not any(r.startswith("WS:") for r in result)


# --- widen_ws_wi_refs ---


def test_widen_no_workstream_id_returns_signal_unchanged() -> None:
    sig = _signal(workstream_id=None)
    ws = _workstream(meeting_wi=(1001,))
    result = widen_ws_wi_refs(sig, (ws,))
    assert result.entity_refs == ()


def test_widen_already_has_wi_refs_returns_unchanged() -> None:
    sig = _signal(entity_refs=("WI:5001", "WS:ws-a"))
    ws = _workstream(meeting_wi=(1001,))
    result = widen_ws_wi_refs(sig, (ws,))
    assert result.entity_refs == ("WI:5001", "WS:ws-a")


def test_widen_workstream_not_found_returns_unchanged() -> None:
    sig = _signal(workstream_id="ws-z")
    ws = _workstream(ws_id="ws-a", meeting_wi=(1001,))
    result = widen_ws_wi_refs(sig, (ws,))
    assert result.entity_refs == ()


def test_widen_workstream_no_signal_sources_returns_unchanged() -> None:
    sig = _signal()
    ws = Workstream(id="ws-a", name="WS A", signal_sources=None)
    result = widen_ws_wi_refs(sig, (ws,))
    assert result.entity_refs == ()


def test_widen_workstream_sources_all_empty_returns_unchanged() -> None:
    sig = _signal()
    ws = _workstream()  # All source tuples are empty
    result = widen_ws_wi_refs(sig, (ws,))
    assert result.entity_refs == ()


def test_widen_adds_wi_refs_from_meeting_series() -> None:
    sig = _signal(entity_refs=("WS:ws-a",))
    ws = _workstream(meeting_wi=(1001, 1002))
    result = widen_ws_wi_refs(sig, (ws,))
    assert "WI:1001" in result.entity_refs
    assert "WI:1002" in result.entity_refs
    assert "WS:ws-a" in result.entity_refs  # original refs preserved


def test_widen_adds_wi_refs_from_teams_chat() -> None:
    sig = _signal(entity_refs=("WS:ws-a",))
    ws = _workstream(chat_wi=(2001,))
    result = widen_ws_wi_refs(sig, (ws,))
    assert "WI:2001" in result.entity_refs


def test_widen_adds_wi_refs_from_email_threads() -> None:
    sig = _signal(entity_refs=("WS:ws-a",))
    ws = _workstream(email_wi=(3001, 3002))
    result = widen_ws_wi_refs(sig, (ws,))
    assert "WI:3001" in result.entity_refs
    assert "WI:3002" in result.entity_refs


def test_widen_deduplicates_wi_ids_across_sources() -> None:
    """Same WI:1001 declared on both meeting series and chat — should appear once."""
    sources = WorkstreamSignalSources(
        teams_meeting_series=(TeamsMeetingSeries(display_name="m", work_item_ids=(1001,)),),
        teams_chats=(TeamsChat(display_name="c", work_item_ids=(1001, 2001)),),
    )
    ws = Workstream(id="ws-a", name="WS A", signal_sources=sources)
    sig = _signal(entity_refs=("WS:ws-a",))
    result = widen_ws_wi_refs(sig, (ws,))
    wi_refs = [r for r in result.entity_refs if r.startswith("WI:")]
    assert wi_refs.count("WI:1001") == 1
    assert "WI:2001" in result.entity_refs


def test_widen_wi_refs_are_sorted() -> None:
    ws = _workstream(meeting_wi=(3000, 1000, 2000))
    sig = _signal()
    result = widen_ws_wi_refs(sig, (ws,))
    wi_refs = [r for r in result.entity_refs if r.startswith("WI:")]
    assert wi_refs == ["WI:1000", "WI:2000", "WI:3000"]


def test_widen_empty_workstreams_tuple_returns_unchanged() -> None:
    sig = _signal()
    result = widen_ws_wi_refs(sig, ())
    assert result.entity_refs == ()


def test_widen_signal_is_frozen_dataclass_returns_new_instance() -> None:
    ws = _workstream(meeting_wi=(4001,))
    sig = _signal(entity_refs=("WS:ws-a",))
    result = widen_ws_wi_refs(sig, (ws,))
    assert result is not sig
    assert "WI:4001" in result.entity_refs


def test_widen_only_ws_ref_no_text_wi_gets_widened() -> None:
    """The canonical FR-SG-08 case: Teams message bound to workstream but no explicit WI text."""
    ws = _workstream(meeting_wi=(9001, 9002))
    sig = _signal(entity_refs=("WS:ws-a",))
    result = widen_ws_wi_refs(sig, (ws,))
    assert any(r.startswith("WI:") for r in result.entity_refs)
    assert "WS:ws-a" in result.entity_refs  # original refs preserved
