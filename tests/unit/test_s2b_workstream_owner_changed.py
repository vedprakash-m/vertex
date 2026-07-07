"""E2E test for S-2b: workstream.owner_changed precondition (workstream.created required).

The bridge must raise a ValueError when a workstream.owner_changed.v1 event arrives
without a prior workstream.created.v1 being accepted — i.e., no existing workstream.entry
fact in the DB.  This is an E2E test: it goes through append_bridged_workstream_event()
and the ProgramFactStore DB layer, not just the payload-building unit.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.core.ledger.fact_bridge import append_bridged_workstream_event
from src.core.ledger.event_log import ConfidenceTier, TemporalConfidence, EventEnvelope
from src.core.ledger.source_refs import OperatorAssertionRef
from src.core.program_fact_store import ProgramFactStore


# ─── helpers ────────────────────────────────────────────────────────────────

def _make_created_event(workstream_id: str = "ws-alpha") -> EventEnvelope:
    return EventEnvelope(
        event_id=f"evt-{workstream_id}-created",
        program_id="test-prog",
        event_type="workstream.created.v1",
        occurred_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        recorded_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="test",
        payload={
            "workstream_id": workstream_id,
            "name": "Alpha Workstream",
            "owner_person_id": "person:alice",
        },
        source_ref=OperatorAssertionRef(
            asserted_by="test",
            asserted_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        ),
        prev_event_hash="sha256:prev",
        content_hash="sha256:content1",
    )


def _make_owner_changed_event(workstream_id: str = "ws-alpha") -> EventEnvelope:
    return EventEnvelope(
        event_id=f"evt-{workstream_id}-owner-changed",
        program_id="test-prog",
        event_type="workstream.owner_changed.v1",
        occurred_at=datetime(2024, 1, 2, tzinfo=timezone.utc),
        recorded_at=datetime(2024, 1, 2, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="test",
        payload={
            "workstream_id": workstream_id,
            "new_owner_person_id": "person:bob",
        },
        source_ref=OperatorAssertionRef(
            asserted_by="test",
            asserted_at=datetime(2024, 1, 2, tzinfo=timezone.utc),
        ),
        prev_event_hash="sha256:prev",
        content_hash="sha256:content2",
    )


# ─── S-2b precondition tests ─────────────────────────────────────────────────

class TestWorkstreamOwnerChangedPrecondition:
    """S-2b: owner_changed requires workstream.created to have been processed first."""

    def test_owner_changed_without_prior_created_raises(self, tmp_path) -> None:
        """E2E: owner_changed with empty DB raises ValueError (precondition violated)."""
        db_root = tmp_path / "db"
        db_root.mkdir()
        event = _make_owner_changed_event()

        with pytest.raises(ValueError, match="requires an existing workstream.entry fact"):
            append_bridged_workstream_event(event, db_root=db_root)

    def test_owner_changed_after_created_succeeds(self, tmp_path) -> None:
        """E2E happy path: created → owner_changed updates the owner field."""
        db_root = tmp_path / "db"
        db_root.mkdir()

        created_event = _make_created_event()
        owner_changed_event = _make_owner_changed_event()

        # First: accept workstream.created
        create_result = append_bridged_workstream_event(created_event, db_root=db_root)
        assert create_result.action == "created"

        # Then: accept workstream.owner_changed — should succeed
        update_result = append_bridged_workstream_event(owner_changed_event, db_root=db_root)
        assert update_result.action in {"updated", "created", "superseded"}

        # Verify the owner was updated
        snapshot = ProgramFactStore("test-prog", db_root=db_root).snapshot(as_of=None)
        workstream_facts = [f for f in snapshot.facts if f.fact_type == "workstream.entry"]
        assert len(workstream_facts) == 1
        assert workstream_facts[0].payload["owner_person_id"] == "person:bob"

    def test_owner_changed_wrong_workstream_id_raises(self, tmp_path) -> None:
        """E2E: owner_changed for a different workstream than the one created raises."""
        db_root = tmp_path / "db"
        db_root.mkdir()

        # Create ws-alpha, but try to change owner of ws-beta
        created_event = _make_created_event("ws-alpha")
        owner_changed_event = _make_owner_changed_event("ws-beta")  # different workstream

        append_bridged_workstream_event(created_event, db_root=db_root)

        with pytest.raises(ValueError, match="requires an existing workstream.entry fact"):
            append_bridged_workstream_event(owner_changed_event, db_root=db_root)

    def test_created_workstream_has_initial_owner(self, tmp_path) -> None:
        """Baseline: workstream.created sets the initial owner_person_id."""
        db_root = tmp_path / "db"
        db_root.mkdir()

        created_event = _make_created_event()
        append_bridged_workstream_event(created_event, db_root=db_root)

        snapshot = ProgramFactStore("test-prog", db_root=db_root).snapshot(as_of=None)
        workstream_facts = [f for f in snapshot.facts if f.fact_type == "workstream.entry"]
        assert len(workstream_facts) == 1
        assert workstream_facts[0].payload["owner_person_id"] == "person:alice"
