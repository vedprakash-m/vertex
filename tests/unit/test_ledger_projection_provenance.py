from __future__ import annotations

from datetime import datetime, timezone

from src.core.ledger.event_log import ConfidenceTier, TemporalConfidence, build_event_envelope
from src.core.ledger.program_views import canonical_projection_dump, project_events_to_sqlite
from src.core.ledger.source_refs import LTDeckRef


def _deck_ref() -> LTDeckRef:
    return LTDeckRef(file_path="deck.pptx", deck_date=datetime(2025, 3, 20, tzinfo=timezone.utc).date(), slide_number=6)


def test_projection_rows_carry_min_confidence_and_temporal_confidence(tmp_path) -> None:
    events = (
        build_event_envelope(
            program_id="acme",
            event_type="risk.raised.v1",
            occurred_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
            recorded_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
            temporal_confidence=TemporalConfidence.APPROXIMATE,
            confidence=ConfidenceTier.AI_EXTRACTED,
            actor="import",
            payload={"risk_id": "risk:r1", "title": "Risk one", "severity": "high"},
            source_ref=_deck_ref(),
        ),
        build_event_envelope(
            program_id="acme",
            event_type="risk.status_changed.v1",
            occurred_at=datetime(2025, 1, 2, tzinfo=timezone.utc),
            recorded_at=datetime(2025, 1, 2, tzinfo=timezone.utc),
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
            actor="import",
            payload={"risk_id": "risk:r1", "new_status": "blocked"},
            source_ref=_deck_ref(),
        ),
    )

    projection_path = tmp_path / "prov.sqlite3"
    project_events_to_sqlite("acme", events, projection_path=projection_path)
    dump = canonical_projection_dump(projection_path)

    assert dump["proj_risk"][0]["min_confidence"] == "ai_extracted"
    assert dump["proj_risk"][0]["min_temporal_confidence"] == "approximate"
