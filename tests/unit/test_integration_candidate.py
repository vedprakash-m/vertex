"""Direct coverage for the extracted integration candidate parse/serialize helpers (D-13)."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import typer

from src.commands.integration_candidate import (
    _candidate_payload,
    _parse_candidate_status,
    _parse_source_ref_kind,
)
from src.core.discovery_intent import SourceCandidateStatus, SourceRefKind


def test_parse_candidate_status_valid_and_normalized() -> None:
    assert _parse_candidate_status("PENDING") == SourceCandidateStatus.PENDING
    assert _parse_candidate_status("  accepted ") == SourceCandidateStatus.ACCEPTED


def test_parse_candidate_status_invalid_raises_bad_parameter() -> None:
    with pytest.raises(typer.BadParameter, match="Unknown candidate status"):
        _parse_candidate_status("nope")


def test_parse_source_ref_kind_aliases() -> None:
    assert _parse_source_ref_kind("meeting") == SourceRefKind.MEETING_SERIES
    assert _parse_source_ref_kind("chat") == SourceRefKind.TEAMS_CHAT
    assert _parse_source_ref_kind("channel") == SourceRefKind.TEAMS_CHANNEL
    assert _parse_source_ref_kind("EMAIL") == SourceRefKind.EMAIL_THREAD


def test_parse_source_ref_kind_invalid_raises() -> None:
    with pytest.raises(typer.BadParameter, match="Unknown source type"):
        _parse_source_ref_kind("carrier-pigeon")


def test_candidate_payload_serializes_without_matches() -> None:
    store = SimpleNamespace(get_intent_matches=lambda cid: [], get_intent=lambda iid: None)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    candidate = SimpleNamespace(
        candidate_id="c1",
        status=SimpleNamespace(value="pending"),
        channel="teams",
        provider_instance_id="prov",
        ref_kind=SimpleNamespace(value="meeting_series"),
        ref_id="r1",
        display_name="Weekly",
        confidence=0.91,
        source_provider="workiq",
        first_discovered_at=now,
        last_seen_at=now,
        decision_reason=None,
    )
    payload = _candidate_payload(store, candidate)  # type: ignore[arg-type]
    assert payload["candidate_id"] == "c1"
    assert payload["status"] == "pending"
    assert payload["ref_kind"] == "meeting_series"
    assert payload["first_discovered_at"] == now.isoformat()
    assert payload["intents"] == []
