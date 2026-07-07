from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.core.ledger.event_log import ConfidenceTier, TemporalConfidence, build_event_envelope
from src.core.ledger.source_refs import LTDeckRef, OperatorAssertionRef
from src.core.projections.program_projection import FieldCandidate, choose_field_winner
from src.core.protection.supersession import apply_supersession


def _deck_ref() -> LTDeckRef:
    return LTDeckRef(file_path="deck.pptx", deck_date=datetime(2025, 3, 20, tzinfo=timezone.utc).date(), slide_number=1)


def test_choose_field_winner_respects_confidence_then_time_then_source_priority() -> None:
    low = build_event_envelope(
        program_id="acme",
        event_type="risk.status_changed.v1",
        occurred_at=datetime(2025, 3, 20, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 6, 10, 9, 0, 0, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.AI_EXTRACTED,
        actor="extractor",
        payload={"risk_id": "risk:r1", "new_status": "active"},
        source_ref=_deck_ref(),
    )
    high = build_event_envelope(
        program_id="acme",
        event_type="risk.status_changed.v1",
        occurred_at=datetime(2025, 3, 19, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 6, 10, 8, 0, 0, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.OPERATOR_CONFIRMED,
        actor="operator",
        payload={"risk_id": "risk:r1", "new_status": "blocked"},
        source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=datetime(2026, 6, 10, 8, 0, 0, tzinfo=timezone.utc)),
    )

    winner = choose_field_winner((FieldCandidate(low, low.payload["new_status"]), FieldCandidate(high, high.payload["new_status"])))

    assert winner is not None
    assert winner.value == "blocked"


def test_choose_field_winner_uses_latest_occurred_at_within_same_tier() -> None:
    earlier = build_event_envelope(
        program_id="acme",
        event_type="risk.status_changed.v1",
        occurred_at=datetime(2025, 3, 19, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 6, 10, 9, 0, 0, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="import",
        payload={"risk_id": "risk:r1", "new_status": "active"},
        source_ref=_deck_ref(),
    )
    later = build_event_envelope(
        program_id="acme",
        event_type="risk.status_changed.v1",
        occurred_at=datetime(2025, 3, 20, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 6, 10, 8, 0, 0, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="import",
        payload={"risk_id": "risk:r1", "new_status": "blocked"},
        source_ref=_deck_ref(),
    )

    winner = choose_field_winner((FieldCandidate(earlier, "active"), FieldCandidate(later, "blocked")))

    assert winner is not None
    assert winner.value == "blocked"


def test_choose_field_winner_uses_source_priority_then_event_id() -> None:
    ado = build_event_envelope(
        program_id="acme",
        event_type="risk.status_changed.v1",
        occurred_at=datetime(2025, 3, 20, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 6, 10, 9, 0, 0, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="import",
        payload={"risk_id": "risk:r1", "new_status": "active"},
        source_ref=_deck_ref(),
    )
    operator = build_event_envelope(
        program_id="acme",
        event_type="risk.status_changed.v1",
        occurred_at=datetime(2025, 3, 20, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 6, 10, 9, 0, 0, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="import",
        payload={"risk_id": "risk:r1", "new_status": "blocked"},
        source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=datetime(2026, 6, 10, 9, 0, 0, tzinfo=timezone.utc)),
    )

    winner = choose_field_winner((FieldCandidate(ado, "active"), FieldCandidate(operator, "blocked")))

    assert winner is not None
    assert winner.value == "blocked"


def test_apply_supersession_handles_correction_chain() -> None:
    original = build_event_envelope(
        program_id="acme",
        event_type="milestone.date_revised.v1",
        occurred_at=datetime(2021, 6, 1, tzinfo=timezone.utc),
        recorded_at=datetime(2021, 6, 2, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="import",
        payload={"milestone_id": "milestone:m1", "new_target_date": "2021-09-30"},
        source_ref=_deck_ref(),
    )
    correction = build_event_envelope(
        program_id="acme",
        event_type="operator.correction.v1",
        occurred_at=datetime(2026, 1, 10, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 1, 10, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.OPERATOR_CONFIRMED,
        actor="operator",
        payload={"corrects_event_id": original.event_id, "corrected_payload": {"milestone_id": "milestone:m1", "new_target_date": "2021-10-15"}, "reason": "corrected"},
        source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=datetime(2026, 1, 10, tzinfo=timezone.utc)),
    )
    correction_of_correction = build_event_envelope(
        program_id="acme",
        event_type="operator.correction.v1",
        occurred_at=datetime(2026, 1, 11, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 1, 11, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.OPERATOR_CONFIRMED,
        actor="operator",
        payload={
            "corrects_event_id": correction.event_id,
            "corrected_payload": {
                "corrects_event_id": original.event_id,
                "corrected_payload": {"milestone_id": "milestone:m1", "new_target_date": "2021-11-01"},
                "reason": "corrected again",
            },
            "reason": "correction of correction",
        },
        source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=datetime(2026, 1, 11, tzinfo=timezone.utc)),
    )

    resolved = apply_supersession((original, correction, correction_of_correction))

    assert resolved[0].payload["new_target_date"] == "2021-11-01"


def test_apply_supersession_rejects_dangling_target() -> None:
    original = build_event_envelope(
        program_id="acme",
        event_type="milestone.date_revised.v1",
        occurred_at=datetime(2021, 6, 1, tzinfo=timezone.utc),
        recorded_at=datetime(2021, 6, 2, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="import",
        payload={"milestone_id": "milestone:m1", "new_target_date": "2021-09-30"},
        source_ref=_deck_ref(),
    )
    dangling = build_event_envelope(
        program_id="acme",
        event_type="operator.correction.v1",
        occurred_at=datetime(2026, 1, 10, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 1, 10, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.OPERATOR_CONFIRMED,
        actor="operator",
        payload={"corrects_event_id": "01MISSINGTARGET0000000000000000", "corrected_payload": {"milestone_id": "milestone:m1", "new_target_date": "2021-10-15"}, "reason": "corrected"},
        source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=datetime(2026, 1, 10, tzinfo=timezone.utc)),
    )

    with pytest.raises(ValueError, match="Dangling supersession target"):
        apply_supersession((original, dangling))