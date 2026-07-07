"""Direct coverage for the extracted integration seed-plan helpers (D-13)."""

from __future__ import annotations

from src.commands.integration_seed_plan import (
    SeedPlanEntry,
    _render_seed_plan,
    _seed_plan_entry_payload,
    _seed_plan_lookup_hints,
    _seed_plan_ref_kind_group,
    _seed_plan_ref_kind_priority,
)
from src.core.discovery_intent import SourceRefKind


def _entry(**over: object) -> SeedPlanEntry:
    base = dict(
        intent_id="i1",
        workstream_id="ws1",
        ref_kind="meeting_series",
        display_name="Weekly Sync",
        derived_state="searching",
        latest_attempt_outcome=None,
        latest_attempt_reason=None,
        latest_attempted_at=None,
        required_ref_field="series_id",
        acceptable_seed_inputs=("seriesMasterId",),
        graph_request_path="/v1.0/me/events",
        seed_command="vertex integration seed-id ...",
        evidence_hint="hint",
    )
    base.update(over)
    return SeedPlanEntry(**base)  # type: ignore[arg-type]


def test_ref_kind_group_collapses_teams() -> None:
    assert _seed_plan_ref_kind_group(SourceRefKind.TEAMS_CHAT) == "teams_conversation"
    assert _seed_plan_ref_kind_group(SourceRefKind.TEAMS_CHANNEL) == "teams_conversation"
    assert _seed_plan_ref_kind_group(SourceRefKind.MEETING_SERIES) == SourceRefKind.MEETING_SERIES.value


def test_ref_kind_priority_orders_meeting_first() -> None:
    assert _seed_plan_ref_kind_priority(SourceRefKind.MEETING_SERIES.value) == 0
    assert _seed_plan_ref_kind_priority(SourceRefKind.TEAMS_CHAT.value) == 1
    assert _seed_plan_ref_kind_priority(SourceRefKind.TEAMS_CHANNEL.value) == 2
    assert _seed_plan_ref_kind_priority("unknown") == 99


def test_lookup_hints_per_ref_kind() -> None:
    field, inputs, path, hint = _seed_plan_lookup_hints(SourceRefKind.MEETING_SERIES, display_name="Weekly Sync")
    assert field == "series_id"
    assert "seriesMasterId" in inputs
    assert "Weekly%20Sync" in path  # url-encoded display name

    field2, _, path2, _ = _seed_plan_lookup_hints(SourceRefKind.TEAMS_CHAT, display_name="X")
    assert field2 == "thread_id"
    assert "/chats" in path2


def test_entry_payload_roundtrip_fields() -> None:
    payload = _seed_plan_entry_payload(_entry())
    assert payload["intent_id"] == "i1"
    assert payload["acceptable_seed_inputs"] == ["seriesMasterId"]  # tuple -> list


def test_render_seed_plan_empty_and_populated() -> None:
    assert "No unresolved source intents" in _render_seed_plan((), program="acme")
    text = _render_seed_plan((_entry(latest_attempt_outcome="not_found", latest_attempted_at="2026-01-01T00:00:00+00:00"),), program="acme")
    assert "Found 1 unresolved source intents" in text
    assert "ws1 | meeting_series | Weekly Sync" in text
    assert "Latest attempt: not_found at 2026-01-01T00:00:00+00:00" in text
    assert "Seed command:" in text
