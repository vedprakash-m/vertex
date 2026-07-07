from __future__ import annotations

from datetime import datetime, timezone

from src.core.ledger.event_index import load_event_entity_refs, load_indexed_events, load_vault_refs, rebuild_event_index
from src.core.ledger.event_log import ConfidenceTier, TemporalConfidence, build_event_envelope, write_event
from src.core.ledger.source_refs import LTDeckRef, OperatorAssertionRef


def test_write_event_populates_event_index(tmp_path) -> None:
    programs_root = tmp_path / "programs"
    source_ref = LTDeckRef(
        file_path="docs/Monthly_LT_Review/2025-03_NOVA_LT_Review.pptx",
        deck_date=datetime(2025, 3, 20, tzinfo=timezone.utc).date(),
        slide_number=14,
        vault_hash="sha256:vault-1",
    )
    corroborating_ref = OperatorAssertionRef(
        asserted_by="operator",
        asserted_at=datetime(2026, 6, 10, 9, 5, 0, tzinfo=timezone.utc),
        vault_hash="sha256:vault-2",
    )
    event = build_event_envelope(
        program_id="acme",
        event_type="risk.raised.v1",
        occurred_at=datetime(2025, 3, 20, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 6, 10, 9, 0, 0, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.AI_EXTRACTED,
        actor="lt_deck_extractor",
        payload={"risk_id": "risk:r1", "title": "Risk one", "severity": "high", "grounded_in": ["claim:1"]},
        source_ref=source_ref,
        corroborating_refs=(corroborating_ref,),
    )

    result = write_event(event, programs_root=programs_root)

    indexed = load_indexed_events("acme", programs_root=programs_root)
    entity_refs = load_event_entity_refs("acme", programs_root=programs_root)
    vault_refs = load_vault_refs("acme", programs_root=programs_root)

    assert indexed[0].event_id == result.envelope.event_id
    assert indexed[0].source_document_key.endswith("2025-03-20:14")
    assert entity_refs[result.envelope.event_id] == ("claim:1", "risk:r1")
    assert vault_refs == (
        ("sha256:vault-1", result.envelope.event_id, "event", "source_ref"),
        ("sha256:vault-2", result.envelope.event_id, "event", "corroborating_ref"),
    )


def test_rebuild_event_index_reconstructs_rows(tmp_path) -> None:
    programs_root = tmp_path / "programs"
    ref = LTDeckRef(file_path="deck.pptx", deck_date=datetime(2025, 3, 20, tzinfo=timezone.utc).date())
    write_event(
        build_event_envelope(
            program_id="acme",
            event_type="milestone.date_revised.v1",
            occurred_at=datetime(2025, 3, 20, tzinfo=timezone.utc),
            recorded_at=datetime(2026, 6, 10, 9, 0, 0, tzinfo=timezone.utc),
            temporal_confidence=TemporalConfidence.APPROXIMATE,
            confidence=ConfidenceTier.AI_EXTRACTED,
            actor="lt_deck_extractor",
            payload={"milestone_id": "milestone:m1", "new_target_date": "2025-09-30"},
            source_ref=ref,
        ),
        programs_root=programs_root,
    )

    rebuilt = rebuild_event_index("acme", programs_root=programs_root)

    assert rebuilt == 1
    assert len(load_indexed_events("acme", programs_root=programs_root)) == 1