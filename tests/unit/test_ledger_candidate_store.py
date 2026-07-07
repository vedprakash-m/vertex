from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import pytest

from src.core.ledger.candidate_store import (
    CandidateDecisionRecord,
    CandidateEntityResolution,
    CandidateEvent,
    active_candidates,
    active_count,
    append_candidate,
    append_triage_decision,
    batch_ids,
    derive_candidate_dedupe_key,
    load_pending_candidates,
    load_triage_decisions,
)
from src.core.ledger.source_refs import EmailRef, LTDeckRef


def _deck_ref() -> LTDeckRef:
    return LTDeckRef(file_path="deck.pptx", deck_date=datetime(2025, 3, 20, tzinfo=timezone.utc).date(), slide_number=9)


def _candidate(candidate_id: str, *, batch_id: str = "batch-1", confidence: float = 0.9) -> CandidateEvent:
    source_document_key = "lt_deck:deck.pptx:2025-03-20:9"
    dedupe_core_hash = "sha256:core"
    return CandidateEvent(
        candidate_id=candidate_id,
        program_id="acme",
        proposed_event_type="milestone.date_revised.v1",
        proposed_payload={"milestone_id": "milestone:m1", "new_target_date": "2025-09-30"},
        proposed_occurred_at=datetime(2025, 3, 20, tzinfo=timezone.utc),
        proposed_temporal_confidence="approximate",
        proposed_confidence="ai_extracted",
        source_ref=_deck_ref(),
        pipeline="lt_deck",
        extraction_confidence=confidence,
        entity_resolution=(
            CandidateEntityResolution(raw_name="Gen9", resolved_entity_id="milestone:m1", match_kind="exact", score=1.0),
        ),
        dedupe_key=derive_candidate_dedupe_key(source_document_key, dedupe_core_hash),
        dedupe_core_hash=dedupe_core_hash,
        source_document_key=source_document_key,
        corroborating_refs=(),
        batch_id=batch_id,
    )


def test_candidate_store_round_trip_and_batch_listing(tmp_path) -> None:
    append_candidate(_candidate("cand-1", batch_id="batch-a"), programs_root=tmp_path)
    append_candidate(_candidate("cand-2", batch_id="batch-b"), programs_root=tmp_path)

    pending = load_pending_candidates("acme", programs_root=tmp_path)

    assert [candidate.candidate_id for candidate in pending] == ["cand-1", "cand-2"]
    assert all(candidate.staged_at is not None for candidate in pending)
    assert batch_ids("acme", programs_root=tmp_path) == ("batch-a", "batch-b")


def test_active_queue_is_derived_from_pending_minus_decisions(tmp_path) -> None:
    append_candidate(_candidate("cand-1", confidence=0.8), programs_root=tmp_path)
    append_candidate(_candidate("cand-2", confidence=0.9), programs_root=tmp_path)
    append_candidate(_candidate("cand-3", confidence=0.7), programs_root=tmp_path)

    append_triage_decision(
        CandidateDecisionRecord(
            candidate_id="cand-1",
            kind="approved",
            decided_at=datetime(2026, 6, 11, tzinfo=timezone.utc),
            triage_actor="operator",
            resulting_event_id="01EVENT",
            edited=False,
        ),
        program_id="acme",
        programs_root=tmp_path,
    )
    append_triage_decision(
        CandidateDecisionRecord(
            candidate_id="cand-2",
            kind="skipped",
            decided_at=datetime(2026, 6, 11, tzinfo=timezone.utc),
            triage_actor="operator",
        ),
        program_id="acme",
        programs_root=tmp_path,
    )

    active = active_candidates("acme", programs_root=tmp_path, as_of=datetime(2026, 6, 12, tzinfo=timezone.utc))

    assert [candidate.candidate_id for candidate in active] == ["cand-2", "cand-3"]
    assert active_count("acme", programs_root=tmp_path, as_of=datetime(2026, 6, 12, tzinfo=timezone.utc)) == 2


def test_expired_skip_is_excluded_from_active_queue(tmp_path) -> None:
    append_candidate(_candidate("cand-1"), programs_root=tmp_path)
    append_triage_decision(
        CandidateDecisionRecord(
            candidate_id="cand-1",
            kind="skipped",
            decided_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            triage_actor="operator",
        ),
        program_id="acme",
        programs_root=tmp_path,
    )

    active = active_candidates("acme", programs_root=tmp_path, as_of=datetime(2026, 4, 5, tzinfo=timezone.utc))

    assert active == ()


def test_triage_decisions_are_auditable(tmp_path) -> None:
    append_candidate(_candidate("cand-1"), programs_root=tmp_path)
    decision = CandidateDecisionRecord(
        candidate_id="cand-1",
        kind="rejected",
        decided_at=datetime(2026, 6, 11, tzinfo=timezone.utc),
        triage_actor="operator",
        reason="bad extraction",
        batch_id="batch-1",
    )
    append_triage_decision(decision, program_id="acme", programs_root=tmp_path)

    decisions = load_triage_decisions("acme", programs_root=tmp_path)

    assert decisions == (decision,)


def test_triage_decision_preserves_approval_event_id(tmp_path) -> None:
    append_candidate(_candidate("cand-approval"), programs_root=tmp_path)
    decision = CandidateDecisionRecord(
        candidate_id="cand-approval",
        kind="approved",
        decided_at=datetime(2026, 6, 11, tzinfo=timezone.utc),
        triage_actor="operator",
        batch_id="batch-1",
        resulting_event_id="evt-result",
        approval_event_id="evt-approval",
    )
    append_triage_decision(decision, program_id="acme", programs_root=tmp_path)

    decisions = load_triage_decisions("acme", programs_root=tmp_path)

    assert decisions == (decision,)


def test_append_candidate_rejects_invalid_source_ref(tmp_path) -> None:
    candidate = _candidate("cand-1")

    with pytest.raises(ValueError, match="source_ref"):
        append_candidate(
            replace(candidate, source_ref=replace(candidate.source_ref, file_path=None)),  # type: ignore[arg-type]
            programs_root=tmp_path,
        )


def test_append_candidate_rejects_external_origin_source_ref_without_vault_hash(tmp_path) -> None:
    candidate = replace(
        _candidate("cand-1"),
        source_ref=EmailRef(
            subject="Escalation",
            sent_at=datetime(2025, 3, 20, 9, 0, 0, tzinfo=timezone.utc),
            sender="owner@example.com",
            message_id="msg-1",
        ),
    )

    with pytest.raises(ValueError, match="vault_hash"):
        append_candidate(candidate, programs_root=tmp_path)
