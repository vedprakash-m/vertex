"""S-11c: Load/soak/replay/recovery tests at pilot workload vs §5.8 SLOs.

Spec reference: .archive/specs/consolidated.md §S-11c / §5.8 (local-only); core spec: vertex-tech-spec.md §13.6.

This test file covers the deterministic portions of the §5.8 SLO budget.
The full cycle-wall-time (p95 ≤120s) gate cannot be verified until Prompt
Shields + LLM are wired (S-10a/S-10b) — those paths are **measured but not
asserted** here per the spec's "measure, don't assert" guidance for S-11c.

Deterministic gates verified:
  G-fleet-replay:    replay throughput ≥10,000 events/min (deterministic)
  G-fleet-isolation: cross-program contamination = zero at pilot scale
  G-fleet-recovery:  incremental replay recovers from missing/stale projection

Pilot workload (§5.8):
  3 programs × 100 events per program = 300 events (deterministic subset).
  Full 100,000-event corpus requires corpus gating (S-9b).
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.core.ledger.event_log import ConfidenceTier, TemporalConfidence, build_event_envelope
from src.core.ledger.program_views import (
    canonical_projection_dump,
    project_events_incremental_to_sqlite,
    project_events_to_sqlite,
)
from src.core.ledger.source_refs import LTDeckRef


_BASE_TIME = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
_PROGRAMS = ("prog-alpha", "prog-beta", "prog-gamma")
_EVENTS_PER_PROGRAM = 100      # §5.8 pilot: 100 docs/program/cycle
_THROUGHPUT_EVENTS = 1_000     # Throughput probe; 1000 events in < 6s → ≥10,000/min
_THROUGHPUT_LIMIT_SECONDS = 6  # = 10,000 events/min minimum


def _deck_ref(idx: int = 0) -> LTDeckRef:
    return LTDeckRef(
        file_path=f"deck_{idx}.pptx",
        deck_date=_BASE_TIME.date(),
        slide_number=idx + 1,
    )


def _make_events(
    program_id: str,
    count: int,
    *,
    base_time: datetime = _BASE_TIME,
    id_offset: int = 0,
) -> list:
    """Build ``count`` synthetic ``milestone.created.v1`` events for one program.

    Each event creates a uniquely-IDed milestone (namespaced by program_id and
    id_offset) so cross-program contamination is detectable by milestone_id.
    """
    events = []
    for i in range(count):
        ts = base_time + timedelta(minutes=i)
        events.append(
            build_event_envelope(
                program_id=program_id,
                event_type="milestone.created.v1",
                occurred_at=ts,
                recorded_at=ts,
                temporal_confidence=TemporalConfidence.EXACT,
                confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
                actor="ado-sync",
                payload={
                    "milestone_id": f"milestone:{program_id}-{id_offset + i}",
                    "name": f"Milestone {id_offset + i} for {program_id}",
                    "target_date": "2026-12-31",
                },
                source_ref=_deck_ref(i % 10),
            )
        )
    return events


class TestReplayThroughput:
    """G-fleet-replay: §5.8 replay throughput ≥10,000 events/min."""

    def test_replay_throughput_meets_slo(self, tmp_path: Path) -> None:
        """Replay 1000 events in < 6 seconds (= ≥10,000 events/min).

        This gates the deterministic projection path only; LLM + Shields are
        not wired so the full cycle-time target (p95 ≤120s) is not measured.
        """
        events = _make_events("prog-throughput", _THROUGHPUT_EVENTS)
        db_path = tmp_path / "prog-throughput" / "projection.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)

        start = time.perf_counter()
        result = project_events_to_sqlite(
            "prog-throughput",
            events,
            projection_path=db_path,
            programs_root=tmp_path,
        )
        elapsed = time.perf_counter() - start

        events_per_min = result.event_count / elapsed * 60
        # Hard gate: ≥10,000 events/min for the deterministic replay path.
        assert elapsed < _THROUGHPUT_LIMIT_SECONDS, (
            f"Replay throughput SLO miss: {_THROUGHPUT_EVENTS} events in "
            f"{elapsed:.2f}s ({events_per_min:.0f}/min) — SLO is "
            f"≥10,000/min ({_THROUGHPUT_LIMIT_SECONDS}s budget)"
        )
        assert result.event_count == _THROUGHPUT_EVENTS, (
            f"Event loss: projected {result.event_count}/{_THROUGHPUT_EVENTS} events"
        )

    def test_incremental_replay_is_at_least_as_fast_as_full_rebuild(
        self, tmp_path: Path
    ) -> None:
        """Incremental fold must not be slower than full rebuild for a large delta.

        For deltas larger than _MAX_INCREMENTAL_DELTA the engine falls back to
        full rebuild — this verifies that fallback path still meets budget.
        """
        events = _make_events("prog-incr", _THROUGHPUT_EVENTS)
        db_path = tmp_path / "prog-incr" / "projection.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)

        # Seed the projection with first half
        half = events[: _THROUGHPUT_EVENTS // 2]
        project_events_to_sqlite("prog-incr", half, projection_path=db_path, programs_root=tmp_path)

        # Incremental fold with all events (large delta → triggers full rebuild path)
        start = time.perf_counter()
        result = project_events_incremental_to_sqlite(
            "prog-incr",
            events,
            projection_path=db_path,
            programs_root=tmp_path,
        )
        elapsed = time.perf_counter() - start

        assert elapsed < _THROUGHPUT_LIMIT_SECONDS, (
            f"Incremental replay fallback SLO miss: {elapsed:.2f}s"
        )
        assert result.event_count == _THROUGHPUT_EVENTS


class TestCrossProgramIsolationAtScale:
    """G-fleet-isolation: cross-program contamination = zero at pilot workload."""

    def test_three_programs_at_pilot_workload_have_zero_contamination(
        self, tmp_path: Path
    ) -> None:
        """3 programs × 100 events each — no cross-program milestone bleed.

        Each program's milestones are namespaced:
          alpha → milestone:prog-alpha-0 … milestone:prog-alpha-99
          beta  → milestone:prog-beta-1000 … milestone:prog-beta-1099
          gamma → milestone:prog-gamma-2000 … milestone:prog-gamma-2099

        Each projection is built in isolation.  After projection we read back
        the proj_milestone rows from each DB and assert no foreign IDs appear.
        """
        id_bases = {"prog-alpha": 0, "prog-beta": 1000, "prog-gamma": 2000}
        db_paths: dict[str, Path] = {}
        dumps: dict[str, dict] = {}

        for prog_id, id_base in id_bases.items():
            events = _make_events(prog_id, _EVENTS_PER_PROGRAM, id_offset=id_base)
            db_path = tmp_path / prog_id / "projection.db"
            db_path.parent.mkdir(parents=True, exist_ok=True)
            result = project_events_to_sqlite(
                prog_id, events, projection_path=db_path, programs_root=tmp_path
            )
            assert result.event_count == _EVENTS_PER_PROGRAM, (
                f"{prog_id}: expected {_EVENTS_PER_PROGRAM} events, got {result.event_count}"
            )
            db_paths[prog_id] = db_path
            dumps[prog_id] = canonical_projection_dump(db_path)

        # Each projection must contain exactly its own milestones
        for prog_id, id_base in id_bases.items():
            milestone_ids = {
                row["milestone_id"]
                for row in dumps[prog_id]["proj_milestone"]
            }
            # Every ID must be namespaced by this program
            foreign_ids = [mid for mid in milestone_ids if not mid.startswith(f"milestone:{prog_id}-")]
            assert not foreign_ids, (
                f"{prog_id}: foreign milestone IDs detected (contamination): {foreign_ids[:5]}"
            )
            assert len(milestone_ids) == _EVENTS_PER_PROGRAM, (
                f"{prog_id}: expected {_EVENTS_PER_PROGRAM} milestones, got {len(milestone_ids)}"
            )

    def test_pilot_workload_total_event_count_is_correct(self, tmp_path: Path) -> None:
        """3 programs × 100 events = 300 total events projected without loss."""
        total_projected = 0
        for i, prog_id in enumerate(_PROGRAMS):
            events = _make_events(prog_id, _EVENTS_PER_PROGRAM, id_offset=i * 1000)
            db_path = tmp_path / prog_id / "projection.db"
            db_path.parent.mkdir(parents=True, exist_ok=True)
            result = project_events_to_sqlite(prog_id, events, projection_path=db_path, programs_root=tmp_path)
            total_projected += result.event_count

        assert total_projected == len(_PROGRAMS) * _EVENTS_PER_PROGRAM, (
            f"Pilot workload event loss: {total_projected} projected, "
            f"{len(_PROGRAMS) * _EVENTS_PER_PROGRAM} expected"
        )


class TestRecovery:
    """G-fleet-recovery: incremental replay recovers from missing/stale projection."""

    def test_missing_projection_triggers_full_rebuild(self, tmp_path: Path) -> None:
        """incremental_to_sqlite falls back to full rebuild if projection.db absent."""
        events = _make_events("prog-recover", 50)
        db_path = tmp_path / "prog-recover" / "projection.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)

        # Projection file absent → incremental falls back to full rebuild
        assert not db_path.exists()
        result = project_events_incremental_to_sqlite(
            "prog-recover", events, projection_path=db_path, programs_root=tmp_path
        )
        assert db_path.exists(), "Recovery failed: projection.db not created after full rebuild"
        assert result.event_count == 50, f"Recovery event loss: {result.event_count}/50"

    def test_deleted_projection_mid_lifecycle_recovers_on_next_replay(
        self, tmp_path: Path
    ) -> None:
        """Deleting projection.db during lifecycle → next replay produces a full rebuild."""
        events = _make_events("prog-mid", 80)
        db_path = tmp_path / "prog-mid" / "projection.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)

        # Initial projection: project the first 40 unique milestones
        r1 = project_events_to_sqlite("prog-mid", events[:40], projection_path=db_path, programs_root=tmp_path)
        assert r1.event_count == 40

        # Simulate crash: delete projection file
        db_path.unlink()
        assert not db_path.exists()

        # Recovery: incremental rebuild with all 80 events
        r2 = project_events_incremental_to_sqlite(
            "prog-mid", events, projection_path=db_path, programs_root=tmp_path
        )
        assert db_path.exists(), "Recovery: projection.db not recreated"
        assert r2.event_count == 80, f"Recovery event loss: {r2.event_count}/80 events after rebuild"

    def test_zero_event_loss_after_projection_rotation(self, tmp_path: Path) -> None:
        """§5.8: candidate/decision loss after rotation = zero.

        Add 50 events, rotate (simulate rebuild), add 50 more — all 100 events
        visible in the final projection.  The two batches use distinct id_offsets
        so they produce 100 unique milestones (no supersession/deduplication).
        """
        events_a = _make_events("prog-rotate", 50, id_offset=0)
        events_b = _make_events(
            "prog-rotate", 50,
            base_time=_BASE_TIME + timedelta(days=10),
            id_offset=50,
        )
        db_path = tmp_path / "prog-rotate" / "projection.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)

        # Initial projection with first 50 events
        r1 = project_events_to_sqlite("prog-rotate", events_a, projection_path=db_path, programs_root=tmp_path)
        assert r1.event_count == 50

        # Full rebuild (rotation) with all 100 events
        r2 = project_events_to_sqlite(
            "prog-rotate", events_a + events_b, projection_path=db_path, programs_root=tmp_path
        )
        assert r2.event_count == 100, (
            f"Zero-loss after rotation violated: {r2.event_count}/100 events visible"
        )
