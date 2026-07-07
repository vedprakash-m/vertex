from __future__ import annotations

from datetime import datetime, timezone

from src.core.ledger.event_log import ConfidenceTier, TemporalConfidence, build_event_envelope
from src.core.ledger.source_refs import LTDeckRef, OperatorAssertionRef
from src.core.projections.program_projection import FieldCandidate, choose_field_winner


def _deck_ref(slide_number: int) -> LTDeckRef:
    return LTDeckRef(file_path="deck.pptx", deck_date=datetime(2025, 3, 20, tzinfo=timezone.utc).date(), slide_number=slide_number)


def _candidate(*, confidence: ConfidenceTier, occurred_at: datetime, source_ref, value: str):
    event = build_event_envelope(
        program_id="acme",
        event_type="risk.status_changed.v1",
        occurred_at=occurred_at,
        recorded_at=occurred_at,
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=confidence,
        actor="import",
        payload={"risk_id": "risk:r1", "new_status": value},
        source_ref=source_ref,
    )
    return FieldCandidate(event, value)


def test_conflict_matrix_prefers_higher_confidence_over_all_other_keys() -> None:
    winner = choose_field_winner(
        (
            _candidate(confidence=ConfidenceTier.AI_EXTRACTED, occurred_at=datetime(2025, 3, 21, tzinfo=timezone.utc), source_ref=_deck_ref(1), value="active"),
            _candidate(confidence=ConfidenceTier.OPERATOR_CONFIRMED, occurred_at=datetime(2025, 3, 20, tzinfo=timezone.utc), source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=datetime(2025, 3, 20, tzinfo=timezone.utc)), value="blocked"),
        )
    )

    assert winner is not None
    assert winner.value == "blocked"


def test_conflict_matrix_uses_event_id_as_final_tiebreak() -> None:
    first = _candidate(confidence=ConfidenceTier.SOURCE_AUTHORITATIVE, occurred_at=datetime(2025, 3, 20, tzinfo=timezone.utc), source_ref=_deck_ref(1), value="active")
    second = _candidate(confidence=ConfidenceTier.SOURCE_AUTHORITATIVE, occurred_at=datetime(2025, 3, 20, tzinfo=timezone.utc), source_ref=_deck_ref(1), value="blocked")

    winner = choose_field_winner((first, second))

    assert winner is not None
    assert winner.event.event_id == max(first.event.event_id, second.event.event_id)