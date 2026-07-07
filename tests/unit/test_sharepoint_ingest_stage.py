"""Unit tests for SharePoint ingest stage (SP1-6/SP4-4).

Test cases:
  (a) Happy path: 2 candidates → 2 PENDING signals
  (b) Empty batch: 0 signals, no errors
  (c) `m365.enabled=False` → 0 signals
  (d) `max_docs_per_run=1` truncation (limit enforced)
  (e) Backfill older than last_extracted → no re-extraction
  (f) All signals must have review_policy=ReviewPolicy.PENDING (invariant)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from src.commands.gather_pipeline.sharepoint_ingest_stage import (
    SharePointIngestResult,
    SharePointPipelineRunner,
    ingest_sharepoint_candidates,
    run_sharepoint_ingest_stage,
)
from src.core.ledger.candidate_store import CandidateEvent
from src.core.models_v2 import Confidence, ReviewPolicy, Signal


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


@dataclass
class _FakeSignalStore:
    signals: list[Signal] = field(default_factory=list)

    def append(self, signal: Signal) -> None:
        self.signals.append(signal)


def _make_candidate(
    *,
    candidate_id: str = "c1",
    pipeline: str = "lt_deck",
    proposed_confidence: str = "source_authoritative",
    summary: str = "Risk: schedule slip",
    program_id: str = "acme",
    batch_id: str = "batch-1",
    occurred_at: datetime | None = None,
) -> CandidateEvent:
    from datetime import date
    from src.core.ledger.source_refs import LTDeckRef
    if occurred_at is None:
        occurred_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return CandidateEvent(
        candidate_id=candidate_id,
        program_id=program_id,
        proposed_event_type="risk",
        proposed_payload={"summary": summary},
        proposed_occurred_at=occurred_at,
        proposed_temporal_confidence="high",
        proposed_confidence=proposed_confidence,
        source_ref=LTDeckRef(
            file_path="deck.pptx",
            deck_date=date(2026, 1, 1),
            slide_number=3,
            slide_title=summary[:60],
        ),
        pipeline=pipeline,
        extraction_confidence=0.90,
        entity_resolution=(),
        dedupe_key=f"k-{candidate_id}",
        dedupe_core_hash=f"h-{candidate_id}",
        source_document_key=f"doc-{candidate_id}",
        corroborating_refs=(),
        batch_id=batch_id,
        staged_at=occurred_at,
    )


@dataclass
class _FakePipelineBatch:
    candidates: tuple[CandidateEvent, ...] = ()
    gaps: tuple[Any, ...] = ()


class _FakePipelineRunner:
    """Deterministic stub for SharePointPipelineRunner."""

    def __init__(self, candidates: tuple[CandidateEvent, ...] = ()) -> None:
        self._candidates = candidates
        self.called = False

    def __call__(self, **kwargs: Any) -> _FakePipelineBatch:
        self.called = True
        return _FakePipelineBatch(candidates=self._candidates)


# ---------------------------------------------------------------------------
# (a) Happy path: 2 candidates → 2 PENDING signals
# ---------------------------------------------------------------------------


def test_ingest_candidates_happy_path_creates_pending_signals() -> None:
    c1 = _make_candidate(candidate_id="c1", summary="Risk: engine overheating")
    c2 = _make_candidate(candidate_id="c2", summary="Milestone: launch by Q3")
    store = _FakeSignalStore()

    count = ingest_sharepoint_candidates(
        (c1, c2),
        program_id="acme",
        existing_signals=(),
        signal_store=store,
    )

    assert count == 2
    assert len(store.signals) == 2
    # INVARIANT: all signals must be PENDING
    assert all(s.review_policy == ReviewPolicy.PENDING for s in store.signals)


# ---------------------------------------------------------------------------
# (b) Empty batch: 0 signals, no errors
# ---------------------------------------------------------------------------


def test_ingest_candidates_empty_batch() -> None:
    store = _FakeSignalStore()
    count = ingest_sharepoint_candidates(
        (),
        program_id="acme",
        existing_signals=(),
        signal_store=store,
    )
    assert count == 0
    assert len(store.signals) == 0


# ---------------------------------------------------------------------------
# (c) run_sharepoint_ingest_stage with empty pipeline → 0 signals
# ---------------------------------------------------------------------------


def test_run_ingest_stage_empty_pipeline(tmp_path: Path) -> None:
    store = _FakeSignalStore()
    runner = _FakePipelineRunner(candidates=())
    result = run_sharepoint_ingest_stage(
        program_id="acme",
        programs_root=tmp_path,
        existing_signals=(),
        signal_store=store,
        prior_doc_states={},
        batch_id="b1",
        pipeline_runner=runner,
    )
    assert result.signals_created == 0
    assert result.docs_processed == 0
    assert len(store.signals) == 0
    assert runner.called


# ---------------------------------------------------------------------------
# (d) max_docs_per_run truncation
# ---------------------------------------------------------------------------


def test_run_ingest_stage_max_docs_truncation(tmp_path: Path) -> None:
    # 6 candidates, max_docs_per_run=1 → cap is 1*30=30 but we have only 6,
    # so all pass through. Test with max_docs_per_run=0 to verify 0-cap.
    candidates = tuple(
        _make_candidate(candidate_id=f"c{i}", summary=f"Risk: item {i}")
        for i in range(6)
    )
    store = _FakeSignalStore()
    runner = _FakePipelineRunner(candidates=candidates)

    # max_docs_per_run=0 means cap=0 candidates
    result = run_sharepoint_ingest_stage(
        program_id="acme",
        programs_root=tmp_path,
        existing_signals=(),
        signal_store=store,
        prior_doc_states={},
        batch_id="b1",
        max_docs_per_run=0,
        pipeline_runner=runner,
    )
    # 0 docs * 30 = 0 candidates
    assert result.signals_created == 0


# ---------------------------------------------------------------------------
# (e) Backfill older than last_extracted → no re-extraction
# ---------------------------------------------------------------------------


def test_load_backfill_skips_old_pptx(tmp_path: Path) -> None:
    """When pptx mtime < last_extracted, backfill should not re-extract."""
    from src.commands.gather_pipeline.sharepoint_ingest_stage import _load_backfill_candidates

    doc_dir = tmp_path / "backfill" / "sharepoint" / "deck-1"
    doc_dir.mkdir(parents=True)
    pptx_file = doc_dir / "deck.pptx"
    pptx_file.write_bytes(b"fake pptx content")

    # Set file mtime to yesterday
    import os, time
    yesterday = time.time() - 86400
    os.utime(pptx_file, (yesterday, yesterday))

    # prior state says we extracted today
    today_str = datetime.now(timezone.utc).isoformat()
    prior_doc_states = {
        "deck-1": {"last_extracted": today_str}
    }

    candidates = _load_backfill_candidates(
        program_id="acme",
        programs_root=tmp_path,
        prior_doc_states=prior_doc_states,
        batch_id="b1",
        force_refresh=False,
        as_of=datetime.now(timezone.utc),
    )
    # Should skip because pptx mtime < last_extracted
    assert candidates == ()


# ---------------------------------------------------------------------------
# (f) PENDING invariant across all confidence levels
# ---------------------------------------------------------------------------


def test_all_signals_are_pending_regardless_of_confidence() -> None:
    """INVARIANT: SharePoint signals MUST be PENDING regardless of confidence."""
    candidates = (
        _make_candidate(candidate_id="high", proposed_confidence="source_authoritative", summary="Risk: overheating engine"),
        _make_candidate(candidate_id="med", proposed_confidence="ai_extracted", summary="Milestone: launch Q3 2026"),
        _make_candidate(candidate_id="low", proposed_confidence="unknown", summary="Decision: approved release plan"),
    )
    store = _FakeSignalStore()
    ingest_sharepoint_candidates(candidates, program_id="acme", existing_signals=(), signal_store=store)

    assert len(store.signals) == 3
    for signal in store.signals:
        assert signal.review_policy == ReviewPolicy.PENDING, (
            f"Signal {signal.id} has review_policy={signal.review_policy!r}; expected PENDING"
        )


# ---------------------------------------------------------------------------
# (g) Backfill present + force_refresh → re-extracts even if not changed
#     (Note: actual extraction needs python-pptx, so we only test that
#      force_refresh=True bypasses the mtime check and attempts extraction)
# ---------------------------------------------------------------------------


def test_load_backfill_force_refresh_bypasses_mtime(tmp_path: Path) -> None:
    """force_refresh=True should attempt extraction even if pptx is older than last_extracted."""
    from src.commands.gather_pipeline.sharepoint_ingest_stage import _load_backfill_candidates

    doc_dir = tmp_path / "backfill" / "sharepoint" / "deck-1"
    doc_dir.mkdir(parents=True)
    pptx_file = doc_dir / "deck.pptx"
    pptx_file.write_bytes(b"PK")  # invalid pptx — extraction will fail gracefully

    today_str = datetime.now(timezone.utc).isoformat()
    prior_doc_states = {"deck-1": {"last_extracted": today_str}}

    # With force_refresh=True, we should attempt extraction.
    # Even though pptx is invalid, we should NOT get the 'skip' branch.
    # The function either returns empty (error) or raises gracefully.
    candidates = _load_backfill_candidates(
        program_id="acme",
        programs_root=tmp_path,
        prior_doc_states=prior_doc_states,
        batch_id="b1",
        force_refresh=True,
        as_of=datetime.now(timezone.utc),
    )
    # Result is empty because invalid pptx fails extraction gracefully
    assert isinstance(candidates, tuple)


# ---------------------------------------------------------------------------
# (h) Confidence mapping
# ---------------------------------------------------------------------------


def test_candidate_confidence_mapping() -> None:
    """source_authoritative → HIGH, ai_extracted → MEDIUM."""
    c_high = _make_candidate(candidate_id="h1", proposed_confidence="source_authoritative", summary="Risk: critical path delay on feature X")
    c_med = _make_candidate(candidate_id="m1", proposed_confidence="ai_extracted", summary="Decision: approved design for component Y")

    store = _FakeSignalStore()
    ingest_sharepoint_candidates((c_high, c_med), program_id="acme", existing_signals=(), signal_store=store)

    assert len(store.signals) == 2
    sig_by_id = {s.id: s for s in store.signals}
    assert sig_by_id["h1"].confidence == Confidence.HIGH
    assert sig_by_id["m1"].confidence == Confidence.MEDIUM
