from __future__ import annotations

from datetime import datetime, timezone

from src.core.ledger.event_log import ConfidenceTier, TemporalConfidence, build_event_envelope
from src.core.ledger.program_views import canonical_projection_dump, project_events_to_sqlite
from src.core.ledger.source_refs import LTDeckRef, OperatorAssertionRef


def _deck_ref() -> LTDeckRef:
    return LTDeckRef(file_path="deck.pptx", deck_date=datetime(2025, 3, 20, tzinfo=timezone.utc).date(), slide_number=7)


def _operator_ref(when: datetime) -> OperatorAssertionRef:
    return OperatorAssertionRef(asserted_by="operator", asserted_at=when)


def test_field_lock_pins_supported_field(tmp_path) -> None:
    created = build_event_envelope(
        program_id="acme",
        event_type="milestone.created.v1",
        occurred_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        recorded_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="import",
        payload={"milestone_id": "milestone:m1", "name": "GA", "target_date": "2025-09-30"},
        source_ref=_deck_ref(),
    )
    locked = build_event_envelope(
        program_id="acme",
        event_type="operator.field_lock.v1",
        occurred_at=datetime(2025, 1, 2, tzinfo=timezone.utc),
        recorded_at=datetime(2025, 1, 2, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.OPERATOR_CONFIRMED,
        actor="operator",
        payload={"entity_id": "milestone:m1", "field": "target_date", "locked_value": "2025-10-15"},
        source_ref=_operator_ref(datetime(2025, 1, 2, tzinfo=timezone.utc)),
    )
    revised = build_event_envelope(
        program_id="acme",
        event_type="milestone.date_revised.v1",
        occurred_at=datetime(2025, 1, 3, tzinfo=timezone.utc),
        recorded_at=datetime(2025, 1, 3, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="import",
        payload={"milestone_id": "milestone:m1", "new_target_date": "2025-11-01"},
        source_ref=_deck_ref(),
    )

    projection_path = tmp_path / "locks.sqlite3"
    project_events_to_sqlite("acme", (created, locked, revised), projection_path=projection_path, as_of=datetime(2025, 1, 4, tzinfo=timezone.utc))
    dump = canonical_projection_dump(projection_path)

    assert dump["proj_milestone"][0]["target_date"] == "2025-10-15"


def test_field_lock_expires_by_as_of(tmp_path) -> None:
    created = build_event_envelope(
        program_id="acme",
        event_type="milestone.created.v1",
        occurred_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        recorded_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="import",
        payload={"milestone_id": "milestone:m1", "name": "GA", "target_date": "2025-09-30"},
        source_ref=_deck_ref(),
    )
    locked = build_event_envelope(
        program_id="acme",
        event_type="operator.field_lock.v1",
        occurred_at=datetime(2025, 1, 2, tzinfo=timezone.utc),
        recorded_at=datetime(2025, 1, 2, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.OPERATOR_CONFIRMED,
        actor="operator",
        payload={"entity_id": "milestone:m1", "field": "target_date", "locked_value": "2025-10-15", "valid_until": "2025-01-03T00:00:00+00:00"},
        source_ref=_operator_ref(datetime(2025, 1, 2, tzinfo=timezone.utc)),
    )
    revised = build_event_envelope(
        program_id="acme",
        event_type="milestone.date_revised.v1",
        occurred_at=datetime(2025, 1, 3, tzinfo=timezone.utc),
        recorded_at=datetime(2025, 1, 3, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="import",
        payload={"milestone_id": "milestone:m1", "new_target_date": "2025-11-01"},
        source_ref=_deck_ref(),
    )

    projection_path = tmp_path / "locks_expired.sqlite3"
    project_events_to_sqlite("acme", (created, locked, revised), projection_path=projection_path, as_of=datetime(2025, 1, 4, tzinfo=timezone.utc))
    dump = canonical_projection_dump(projection_path)

    assert dump["proj_milestone"][0]["target_date"] == "2025-11-01"