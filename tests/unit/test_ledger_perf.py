"""NFR-3 performance gate: 50K event log full replay must complete in < 60 s (§9.5, S2 accept).

Run via:  pytest -m slow tests/unit/test_ledger_perf.py
CI:       included on every merge to main.
Dev loop: excluded via -m "not slow" to keep the inner-loop fast.
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.core.ledger.event_log import (
    ConfidenceTier,
    TemporalConfidence,
    build_event_envelope,
    write_events_atomic,
)
from src.core.ledger.program_views import project_program_events
from src.core.ledger.source_refs import LTDeckRef


_DECK_REF = LTDeckRef(
    file_path="perf_deck.pptx",
    deck_date=date(2025, 1, 1),
    slide_number=1,
)

_EVENT_SPECS: tuple[tuple[str, object], ...] = (
    ("risk.status_changed.v1", {"risk_id": "risk:PLACEHOLDER", "new_status": "active"}),
    ("milestone.date_revised.v1", {"milestone_id": "milestone:PLACEHOLDER", "new_target_date": "2025-09-30"}),
    ("metric.observed.v1", {"kpi_id": "kpi:PLACEHOLDER", "value": 0.0, "unit": "count"}),
    ("dependency.status_changed.v1", {"dependency_id": "dependency:PLACEHOLDER", "new_status": "resolved"}),
)


def _build_payload(event_type: str, template: dict, i: int) -> dict:  # type: ignore[type-arg]
    payload = dict(template)
    for key, value in payload.items():
        if isinstance(value, str) and "PLACEHOLDER" in value:
            payload[key] = value.replace("PLACEHOLDER", str(i % 300))
    if event_type == "metric.observed.v1":
        payload["value"] = float(i % 1000)
    return payload


@pytest.mark.slow
def test_replay_perf_50k(tmp_path: Path) -> None:
    """Full-log replay of a 50K-event synthetic program must complete in < 60 s (NFR-3).

    Strategy: build all envelopes in memory, write in a single atomic batch
    (O(n) writes), then measure project_program_events wall time.
    """
    programs_root = tmp_path / "programs"
    program_id = "perf_nova"

    n = 50_000
    base_occurred = datetime(2020, 1, 1, tzinfo=timezone.utc)
    base_recorded = datetime(2026, 1, 1, tzinfo=timezone.utc)

    envelopes = []
    for i in range(n):
        event_type, template = _EVENT_SPECS[i % len(_EVENT_SPECS)]
        occurred_at = base_occurred + timedelta(hours=i)
        recorded_at = base_recorded + timedelta(milliseconds=i)
        envelope = build_event_envelope(
            program_id=program_id,
            event_type=event_type,
            occurred_at=occurred_at,
            recorded_at=recorded_at,
            temporal_confidence=TemporalConfidence.APPROXIMATE,
            confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
            actor="import",
            payload=_build_payload(event_type, template, i),  # type: ignore[arg-type]
            source_ref=_DECK_REF,
        )
        envelopes.append(envelope)

    # Single atomic write — O(n) — reads 0 existing events
    write_events_atomic(
        tuple(envelopes),
        programs_root=programs_root,
        max_bytes=512 * 1024 * 1024,  # 512 MB cap: no mid-batch rotation
    )

    # Measure full projection replay (NFR-3 budget: < 60 s)
    start = time.monotonic()
    project_program_events(program_id, programs_root=programs_root)
    elapsed = time.monotonic() - start

    assert elapsed < 60.0, (
        f"50K event replay took {elapsed:.2f}s, exceeds the 60s NFR-3 budget; "
        "check projection algorithm for O(n²) regressions"
    )
