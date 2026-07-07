from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.core.ledger.candidate_store import CandidateEntityResolution, CandidateEvent, append_candidate
from src.core.ledger.discovery_run_recorder import DiscoveryRunResult, GapDetail, record_discovery_run
from src.core.ledger.event_log import read_events
from src.core.ledger.source_refs import LTDeckRef


def test_record_discovery_run_writes_gap_and_candidate_summary_events(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    append_candidate(_candidate("cand-1", batch_id="batch-42"), programs_root=programs_root)
    append_candidate(_candidate("cand-2", batch_id="batch-42", milestone_id="milestone:m2"), programs_root=programs_root)

    written = record_discovery_run(
        "acme",
        DiscoveryRunResult(
            pipeline="lt_deck",
            batch_id="batch-42",
            candidates_written=2,
            gaps=(
                GapDetail(
                    gap_kind="missing_series_registration",
                    window_start=datetime(2026, 6, 1, tzinfo=timezone.utc),
                    window_end=datetime(2026, 6, 7, tzinfo=timezone.utc),
                    detail="Missing series registration for LT deck ingest.",
                ),
            ),
            heartbeat=False,
        ),
        recorded_at=datetime(2026, 6, 11, 8, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
    )

    assert [event.event_type for event in written] == ["pipeline.gap_detected.v1", "discovery.candidate_proposed.v1"]

    events = read_events("acme", programs_root=programs_root)
    assert [event.event_type for event in events] == ["pipeline.gap_detected.v1", "discovery.candidate_proposed.v1"]
    assert events[0].payload == {
        "pipeline": "lt_deck",
        "gap_kind": "missing_series_registration",
        "detail": "Missing series registration for LT deck ingest.",
        "window_start": "2026-06-01T00:00:00+00:00",
        "window_end": "2026-06-07T00:00:00+00:00",
    }
    assert events[1].payload == {
        "batch_id": "batch-42",
        "pipeline": "lt_deck",
        "candidate_count": 2,
        "event_type_histogram": {"milestone.date_revised.v1": 2},
    }


def test_record_discovery_run_healthy_silence_writes_nothing(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"

    written = record_discovery_run(
        "acme",
        DiscoveryRunResult(
            pipeline="newsletter",
            batch_id="batch-empty",
            candidates_written=0,
            gaps=(),
            heartbeat=True,
        ),
        recorded_at=datetime(2026, 6, 11, 9, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
    )

    assert written == ()
    assert read_events("acme", programs_root=programs_root) == ()


def _candidate(candidate_id: str, *, batch_id: str, milestone_id: str = "milestone:m1") -> CandidateEvent:
    return CandidateEvent(
        candidate_id=candidate_id,
        program_id="acme",
        proposed_event_type="milestone.date_revised.v1",
        proposed_payload={"milestone_id": milestone_id, "new_target_date": "2025-09-30"},
        proposed_occurred_at=datetime(2025, 3, 20, tzinfo=timezone.utc),
        proposed_temporal_confidence="approximate",
        proposed_confidence="ai_extracted",
        source_ref=LTDeckRef(file_path="deck.pptx", deck_date=datetime(2025, 3, 20, tzinfo=timezone.utc).date(), slide_number=9),
        pipeline="lt_deck",
        extraction_confidence=0.9,
        entity_resolution=(
            CandidateEntityResolution(raw_name="Gen9", resolved_entity_id=milestone_id, match_kind="exact", score=1.0),
        ),
        dedupe_key=f"sha256:{candidate_id}",
        dedupe_core_hash="sha256:core",
        source_document_key="lt_deck:deck.pptx:2025-03-20:9",
        corroborating_refs=(),
        batch_id=batch_id,
    )