from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from src.core.ledger.candidate_store import load_pending_candidates
from src.core.ledger.event_log import ConfidenceTier, EventEnvelope, TemporalConfidence, build_event_envelope, write_event
from src.core.ledger.source_refs import OperatorAssertionRef


PROGRAMS_ROOT = Path(__file__).resolve().parents[3] / "programs"


@dataclass(frozen=True, slots=True)
class GapDetail:
    gap_kind: str
    window_start: datetime | None
    window_end: datetime | None
    detail: str


@dataclass(frozen=True, slots=True)
class DiscoveryRunResult:
    pipeline: str
    batch_id: str
    candidates_written: int
    gaps: tuple[GapDetail, ...]
    heartbeat: bool


def record_discovery_run(
    program_id: str,
    result: DiscoveryRunResult,
    *,
    actor: str = "discovery_run_recorder",
    recorded_at: datetime | None = None,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[EventEnvelope, ...]:
    now = recorded_at.astimezone(timezone.utc) if recorded_at is not None else datetime.now(timezone.utc)
    written: list[EventEnvelope] = []
    for gap in result.gaps:
        occurred_at = gap.window_end or gap.window_start or now
        envelope = build_event_envelope(
            program_id=program_id,
            event_type="pipeline.gap_detected.v1",
            occurred_at=occurred_at,
            recorded_at=now,
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.OPERATOR_CONFIRMED,
            actor=actor,
            payload={
                "pipeline": result.pipeline,
                "gap_kind": gap.gap_kind,
                "detail": gap.detail,
                **({"window_start": gap.window_start.isoformat()} if gap.window_start is not None else {}),
                **({"window_end": gap.window_end.isoformat()} if gap.window_end is not None else {}),
            },
            source_ref=OperatorAssertionRef(asserted_by=actor, asserted_at=now, context=f"pipeline={result.pipeline};batch={result.batch_id}"),
            dedupe_payload={
                "pipeline": result.pipeline,
                "gap_kind": gap.gap_kind,
                **({"window_start": gap.window_start.isoformat()} if gap.window_start is not None else {}),
                **({"window_end": gap.window_end.isoformat()} if gap.window_end is not None else {}),
            },
        )
        written.append(write_event(envelope, programs_root=programs_root).envelope)
    if result.candidates_written > 0:
        histogram = _event_type_histogram(program_id, batch_id=result.batch_id, programs_root=programs_root)
        payload: dict[str, object] = {
            "batch_id": result.batch_id,
            "pipeline": result.pipeline,
            "candidate_count": result.candidates_written,
        }
        if histogram:
            payload["event_type_histogram"] = histogram
        envelope = build_event_envelope(
            program_id=program_id,
            event_type="discovery.candidate_proposed.v1",
            occurred_at=now,
            recorded_at=now,
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.OPERATOR_CONFIRMED,
            actor=actor,
            payload=payload,
            source_ref=OperatorAssertionRef(asserted_by=actor, asserted_at=now, context=f"pipeline={result.pipeline};batch={result.batch_id}"),
            dedupe_payload={"batch_id": result.batch_id},
        )
        written.append(write_event(envelope, programs_root=programs_root).envelope)
    return tuple(written)


def _event_type_histogram(program_id: str, *, batch_id: str, programs_root: Path) -> dict[str, int]:
    histogram: dict[str, int] = {}
    for candidate in load_pending_candidates(program_id, programs_root=programs_root):
        if candidate.batch_id != batch_id:
            continue
        histogram[candidate.proposed_event_type] = histogram.get(candidate.proposed_event_type, 0) + 1
    return dict(sorted(histogram.items()))