from __future__ import annotations

from datetime import datetime, timezone
import json

from src.core.ledger.event_log import ConfidenceTier, TemporalConfidence, build_event_envelope
from src.core.ledger.program_views import canonical_projection_dump, project_events_to_sqlite
from src.core.ledger.source_refs import LTDeckRef


def _deck_ref() -> LTDeckRef:
    return LTDeckRef(file_path="deck.pptx", deck_date=datetime(2025, 3, 20, tzinfo=timezone.utc).date(), slide_number=2)


def test_decision_assumption_dependency_folds(tmp_path) -> None:
    events = (
        build_event_envelope(
            program_id="acme",
            event_type="decision.made.v1",
            occurred_at=datetime(2025, 3, 20, tzinfo=timezone.utc),
            recorded_at=datetime(2025, 3, 21, tzinfo=timezone.utc),
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
            actor="import",
            payload={"decision_id": "decision:d1", "title": "Ship", "decision_text": "Ship it", "decided_by": ["operator"], "forum": "LT"},
            source_ref=_deck_ref(),
        ),
        build_event_envelope(
            program_id="acme",
            event_type="decision.revised.v1",
            occurred_at=datetime(2025, 3, 22, tzinfo=timezone.utc),
            recorded_at=datetime(2025, 3, 23, tzinfo=timezone.utc),
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.OPERATOR_CONFIRMED,
            actor="operator",
            payload={"decision_id": "decision:d1", "revision_text": "Ship after mitigation", "reason": "Need one more check"},
            source_ref=_deck_ref(),
        ),
        build_event_envelope(
            program_id="acme",
            event_type="assumption.stated.v1",
            occurred_at=datetime(2025, 3, 20, tzinfo=timezone.utc),
            recorded_at=datetime(2025, 3, 21, tzinfo=timezone.utc),
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
            actor="import",
            payload={"assumption_id": "assumption:a1", "statement": "Capacity holds"},
            source_ref=_deck_ref(),
        ),
        build_event_envelope(
            program_id="acme",
            event_type="assumption.invalidated.v1",
            occurred_at=datetime(2025, 3, 24, tzinfo=timezone.utc),
            recorded_at=datetime(2025, 3, 24, tzinfo=timezone.utc),
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
            actor="import",
            payload={"assumption_id": "assumption:a1", "evidence": "Load test failed", "impact": "Need redesign"},
            source_ref=_deck_ref(),
        ),
        build_event_envelope(
            program_id="acme",
            event_type="dependency.declared.v1",
            occurred_at=datetime(2025, 3, 20, tzinfo=timezone.utc),
            recorded_at=datetime(2025, 3, 21, tzinfo=timezone.utc),
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
            actor="import",
            payload={"dependency_id": "dependency:dep1", "from_entity": "workstream:w1", "to_entity": "milestone:m1", "description": "Milestone needs workstream", "needed_by": "2025-04-01"},
            source_ref=_deck_ref(),
        ),
        build_event_envelope(
            program_id="acme",
            event_type="dependency.status_changed.v1",
            occurred_at=datetime(2025, 3, 25, tzinfo=timezone.utc),
            recorded_at=datetime(2025, 3, 25, tzinfo=timezone.utc),
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
            actor="import",
            payload={"dependency_id": "dependency:dep1", "new_status": "blocked"},
            source_ref=_deck_ref(),
        ),
    )

    projection_path = tmp_path / "entities.sqlite3"
    project_events_to_sqlite("acme", events, projection_path=projection_path)
    dump = canonical_projection_dump(projection_path)

    assert dump["proj_decision"][0]["decision_id"] == "decision:d1"
    assert dump["proj_decision"][0]["decision_text"] == "Ship after mitigation"
    assert dump["proj_decision"][0]["status"] == "revised"
    assert json.loads(dump["proj_decision"][0]["decided_by"]) == ["operator"]

    assert dump["proj_assumption"][0]["assumption_id"] == "assumption:a1"
    assert dump["proj_assumption"][0]["status"] == "invalidated"
    assert dump["proj_assumption"][0]["impact"] == "Need redesign"

    assert dump["proj_dependency"][0]["dependency_id"] == "dependency:dep1"
    assert dump["proj_dependency"][0]["status"] == "blocked"
    assert dump["proj_dependency"][0]["from_entity"] == "workstream:w1"