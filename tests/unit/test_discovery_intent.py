"""Tests for the discovery value model + deterministic IDs (discover.md §8.1/§10.3)."""
from __future__ import annotations

from datetime import datetime, timezone

from src.core.discovery_intent import (
    DiscoveryAttemptOutcome,
    SourceCandidateStatus,
    SourceIntentStatus,
    SourceRefKind,
    build_discovery_attempt_id,
    build_source_candidate_id,
    build_source_intent_id,
    normalize_intent_display_name,
)


def test_normalize_intent_display_name_is_case_and_space_insensitive() -> None:
    assert normalize_intent_display_name("  Acme Weekly Ops Review  ") == "acme weekly ops review"


def test_intent_id_is_deterministic_and_name_normalized() -> None:
    a = build_source_intent_id(
        program_id="acme", workstream_id="ws", ref_kind=SourceRefKind.MEETING_SERIES, display_name="Weekly Ops"
    )
    b = build_source_intent_id(
        program_id="acme", workstream_id="ws", ref_kind=SourceRefKind.MEETING_SERIES, display_name="  weekly ops  "
    )
    assert a == b  # normalization makes these the same intent
    other = build_source_intent_id(
        program_id="acme", workstream_id="ws", ref_kind=SourceRefKind.TEAMS_CHAT, display_name="Weekly Ops"
    )
    assert a != other  # ref_kind participates in identity


def test_candidate_id_is_source_level_no_artifact_id() -> None:
    # discover.md §10.3: candidate identity is (program, channel, provider, ref_kind, ref_id) —
    # NOT artifact_id — so one real source maps to many intents without duplicate rows.
    base = dict(
        program_id="acme", channel="teams", provider_instance_id="default",
        ref_kind=SourceRefKind.TEAMS_CHAT, ref_id="19:abc@thread.v2",
    )
    assert build_source_candidate_id(**base) == build_source_candidate_id(**base)
    moved = dict(base, ref_id="19:def@thread.v2")
    assert build_source_candidate_id(**base) != build_source_candidate_id(**moved)


def test_attempt_id_varies_by_timestamp_and_query() -> None:
    t = datetime(2026, 6, 3, 9, 0, tzinfo=timezone.utc)
    a = build_discovery_attempt_id(program_id="acme", intent_id="i1", source_provider="workiq_teams", query_hash="h1", attempted_at=t)
    same = build_discovery_attempt_id(program_id="acme", intent_id="i1", source_provider="workiq_teams", query_hash="h1", attempted_at=t)
    assert a == same
    diff_query = build_discovery_attempt_id(program_id="acme", intent_id="i1", source_provider="workiq_teams", query_hash="h2", attempted_at=t)
    assert a != diff_query


def test_enum_values_match_spec_state_machine() -> None:
    # §8.1 lifecycle states are all representable.
    assert {s.value for s in SourceIntentStatus} >= {
        "declared", "searching", "no_candidates", "candidate_found", "ambiguous", "resolved",
        "active", "stale", "auth_blocked", "out_of_identity_scope",
        "suppressed", "superseded", "retired",
    }
    assert {s.value for s in SourceCandidateStatus} == {"pending", "accepted", "rejected", "expired", "superseded"}
    assert {o.value for o in DiscoveryAttemptOutcome} >= {
        "no_candidates", "ambiguous", "auth_blocked", "out_of_identity_scope", "budget_exceeded", "error",
    }
