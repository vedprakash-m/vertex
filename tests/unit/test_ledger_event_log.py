from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
import pytest

from src.core.ledger.event_log import (
    ConfidenceTier,
    TemporalConfidence,
    build_event_envelope,
    compute_envelope_hash,
    get_active_event_log_path,
    get_event_logs_dir,
    read_events,
    verify_event_log,
    write_event,
)
from src.core.ledger.event_index import load_indexed_events
from src.core.ledger.event_types import count_control_event_types, get_registered_event_types, validate_event_payload
from src.core.ledger.source_refs import EmailRef, KnowledgeDocumentRef, LTDeckRef, NewsletterRef, source_document_key, source_ref_from_dict, source_ref_to_dict
from src.core.ledger.ulid import new_ulid, reset_ulid_state_for_tests


def test_ulid_clamps_clock_regression() -> None:
    reset_ulid_state_for_tests()
    later = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)
    earlier = datetime(2026, 6, 10, 11, 59, 59, tzinfo=timezone.utc)

    first = new_ulid(later)
    second = new_ulid(earlier)

    assert second > first


def test_source_ref_round_trip_and_document_key_fallback() -> None:
    ref = NewsletterRef(
        file_path="docs/newsletters/NOVA_newsletters/issue_051.eml",
        publication_date=datetime(2025, 2, 1, tzinfo=timezone.utc).date(),
        issue_number=None,
        section="top",
    )

    payload = source_ref_to_dict(ref)
    round_tripped = source_ref_from_dict(payload)

    assert round_tripped == ref
    assert source_document_key(ref) == "newsletter:docs/newsletters/NOVA_newsletters/issue_051.eml:2025-02-01"


def test_knowledge_document_source_ref_round_trip() -> None:
    ref = KnowledgeDocumentRef(
        vault_hash="sha256:abc123",
        original_filename="dd-acme-kb.md",
        origin_kind="local_path",
        origin_path="C:/kb/dd-acme-kb.md",
        origin_url=None,
        ingested_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        section="line:12",
    )

    payload = source_ref_to_dict(ref)
    round_tripped = source_ref_from_dict(payload)

    assert round_tripped == ref
    assert source_document_key(ref) == "knowledge_document:sha256:abc123:line:12"


def test_event_registry_count_and_control_split() -> None:
    # v2.22 (ADR-0006 R2): +4 deployment/incident lifecycle types → 56 total.
    # v1.6 activation: +1 discovery.candidate_revoked.v1 (AG-10 revoke audit) → 57 total.
    assert len(get_registered_event_types()) == 57
    assert count_control_event_types() == (54, 3)


def test_validate_event_payload_rejects_missing_required_field() -> None:
    try:
        validate_event_payload("milestone.date_revised.v1", {"milestone_id": "milestone:gen9"})
    except ValueError as error:
        assert "new_target_date" in str(error)
    else:
        raise AssertionError("Expected missing required field validation failure")


def test_event_log_round_trip_and_verify(tmp_path) -> None:
    programs_root = tmp_path / "programs"
    ref = LTDeckRef(
        file_path="docs/Monthly_LT_Review/2025-03_NOVA_LT_Review.pptx",
        deck_date=datetime(2025, 3, 20, tzinfo=timezone.utc).date(),
        slide_number=14,
        slide_title="Gen9 Rollout Status",
    )
    event = build_event_envelope(
        program_id="acme",
        event_type="milestone.date_revised.v1",
        occurred_at=datetime(2025, 3, 20, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 6, 10, 9, 0, 0, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.APPROXIMATE,
        confidence=ConfidenceTier.AI_EXTRACTED,
        actor="lt_deck_extractor",
        payload={"milestone_id": "milestone:gen9", "new_target_date": "2025-09-30"},
        source_ref=ref,
        dedupe_payload={"milestone_id": "milestone:gen9", "new_target_date": "2025-09-30"},
    )

    result = write_event(event, programs_root=programs_root)

    assert result.envelope.prev_event_hash.startswith("sha256:")
    assert result.envelope.content_hash.startswith("sha256:")
    assert result.envelope.dedupe_core_hash is not None
    assert read_events("acme", programs_root=programs_root) == (result.envelope,)
    assert load_indexed_events("acme", programs_root=programs_root)[0].event_id == result.envelope.event_id
    assert verify_event_log("acme", programs_root=programs_root).ok is True


def test_build_event_envelope_rejects_invalid_source_ref() -> None:
    ref = LTDeckRef(file_path="deck.pptx", deck_date=datetime(2025, 3, 20, tzinfo=timezone.utc).date())

    valid = build_event_envelope(
        program_id="acme",
        event_type="risk.raised.v1",
        occurred_at=datetime(2025, 3, 20, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 6, 10, 9, 0, 0, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="import",
        payload={"risk_id": "risk:r1", "title": "Risk one", "severity": "high"},
        source_ref=ref,
    )

    with pytest.raises(ValueError, match="source_ref"):
        build_event_envelope(
            program_id=valid.program_id,
            event_type=valid.event_type,
            occurred_at=valid.occurred_at,
            recorded_at=valid.recorded_at,
            temporal_confidence=valid.temporal_confidence,
            confidence=valid.confidence,
            actor=valid.actor,
            payload=valid.payload,
            source_ref=replace(ref, file_path=None),  # type: ignore[arg-type]
        )


def test_build_event_envelope_rejects_external_origin_source_ref_without_vault_hash() -> None:
    with pytest.raises(ValueError, match="vault_hash"):
        build_event_envelope(
            program_id="acme",
            event_type="risk.raised.v1",
            occurred_at=datetime(2025, 3, 20, tzinfo=timezone.utc),
            recorded_at=datetime(2026, 6, 10, 9, 0, 0, tzinfo=timezone.utc),
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
            actor="import",
            payload={"risk_id": "risk:r1", "title": "Risk one", "severity": "high"},
            source_ref=EmailRef(
                subject="Escalation",
                sent_at=datetime(2025, 3, 20, 9, 0, 0, tzinfo=timezone.utc),
                sender="owner@example.com",
                message_id="msg-1",
            ),
        )


def test_write_event_clamps_recorded_at_to_previous_event(tmp_path) -> None:
    programs_root = tmp_path / "programs"
    ref = LTDeckRef(file_path="deck.pptx", deck_date=datetime(2025, 3, 20, tzinfo=timezone.utc).date())
    first = build_event_envelope(
        program_id="acme",
        event_type="risk.raised.v1",
        occurred_at=datetime(2025, 3, 20, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 6, 10, 10, 0, 0, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="import",
        payload={"risk_id": "risk:r1", "title": "Risk one", "severity": "high"},
        source_ref=ref,
    )
    second = build_event_envelope(
        program_id="acme",
        event_type="risk.status_changed.v1",
        occurred_at=datetime(2025, 3, 21, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 6, 10, 9, 0, 0, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="import",
        payload={"risk_id": "risk:r1", "new_status": "open"},
        source_ref=ref,
    )

    first_written = write_event(first, programs_root=programs_root)
    second_written = write_event(second, programs_root=programs_root)

    assert second_written.envelope.recorded_at == first_written.envelope.recorded_at
    assert second_written.envelope.prev_event_hash == compute_envelope_hash(first_written.envelope)


def test_torn_line_is_quarantined_and_valid_events_survive(tmp_path) -> None:
    programs_root = tmp_path / "programs"
    ref = LTDeckRef(file_path="deck.pptx", deck_date=datetime(2025, 3, 20, tzinfo=timezone.utc).date())
    event = build_event_envelope(
        program_id="acme",
        event_type="pipeline.gap_detected.v1",
        occurred_at=datetime(2026, 6, 10, 3, 0, 0, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 6, 10, 3, 0, 5, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="workiq_pipeline",
        payload={"pipeline": "workiq", "gap_kind": "null_ids", "detail": "weekly yield [0,0,0]"},
        source_ref=ref,
    )
    write_event(event, programs_root=programs_root)

    current_path = get_active_event_log_path("acme", recorded_at=event.recorded_at, programs_root=programs_root)
    with current_path.open("a", encoding="utf-8") as handle:  # noqa: PB37
        handle.write('{"broken":')

    reloaded = read_events("acme", programs_root=programs_root)
    quarantine_dir = get_event_logs_dir("acme", programs_root=programs_root) / "quarantine"

    assert len(reloaded) == 1
    assert any(quarantine_dir.glob("*.jsonl"))


def test_rotation_keeps_chain_verifiable(tmp_path) -> None:
    programs_root = tmp_path / "programs"
    ref = LTDeckRef(file_path="deck.pptx", deck_date=datetime(2025, 3, 20, tzinfo=timezone.utc).date())

    first = build_event_envelope(
        program_id="acme",
        event_type="risk.raised.v1",
        occurred_at=datetime(2025, 3, 20, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 6, 10, 10, 0, 0, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="import",
        payload={"risk_id": "risk:r1", "title": "Risk one", "severity": "high"},
        source_ref=ref,
    )
    second = build_event_envelope(
        program_id="acme",
        event_type="risk.status_changed.v1",
        occurred_at=datetime(2025, 3, 21, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 6, 10, 10, 1, 0, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="import",
        payload={"risk_id": "risk:r1", "new_status": "blocked"},
        source_ref=ref,
    )

    write_event(first, programs_root=programs_root, max_bytes=1)
    write_event(second, programs_root=programs_root, max_bytes=1)

    verification = verify_event_log("acme", programs_root=programs_root)
    event_files = sorted(get_event_logs_dir("acme", programs_root=programs_root).glob("*.events.jsonl"))

    assert verification.ok is True
    assert len(event_files) >= 2


def test_verify_detects_tamper(tmp_path) -> None:
    programs_root = tmp_path / "programs"
    ref = LTDeckRef(file_path="deck.pptx", deck_date=datetime(2025, 3, 20, tzinfo=timezone.utc).date())
    event = build_event_envelope(
        program_id="acme",
        event_type="risk.raised.v1",
        occurred_at=datetime(2025, 3, 20, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 6, 10, 10, 0, 0, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="import",
        payload={"risk_id": "risk:r1", "title": "Risk one", "severity": "high"},
        source_ref=ref,
    )
    result = write_event(event, programs_root=programs_root)
    path = result.path
    payload = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    payload["payload"]["title"] = "Tampered"
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    verification = verify_event_log("acme", programs_root=programs_root)

    assert verification.ok is False
    assert any("content hash mismatch" in issue for issue in verification.issues)


def test_unknown_event_type_is_rejected_at_write_time(tmp_path) -> None:
    ref = LTDeckRef(file_path="deck.pptx", deck_date=datetime(2025, 3, 20, tzinfo=timezone.utc).date())
    with pytest.raises(ValueError, match="Unknown ledger event type"):
        build_event_envelope(
            program_id="acme",
            event_type="unknown.event.v1",
            occurred_at=datetime(2025, 3, 20, tzinfo=timezone.utc),
            recorded_at=datetime(2026, 6, 10, 10, 0, 0, tzinfo=timezone.utc),
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
            actor="import",
            payload={"value": "nope"},
            source_ref=ref,
        )