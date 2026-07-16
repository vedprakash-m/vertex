"""ADF-W1.5/ADF-W1.10 remainder: src/commands/gather_pipeline/workiq_prefetch_stage.py."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.commands.gather_pipeline.workiq_prefetch_stage import (
    WORKIQ_PREFETCH_CHANNEL,
    resolve_workiq_signals,
    workiq_signals_from_payload,
    workiq_signals_to_payload,
)
from src.core.models import Confidence
from src.core.models_v2 import Signal
from src.core.prefetch_store import write_prefetch_snapshot

_NOW = datetime(2026, 7, 13, 12, 0, 0, tzinfo=timezone.utc)


def _signal(signal_id: str = "sig-1") -> Signal:
    return Signal(
        id=signal_id,
        timestamp=_NOW,
        source="workiq",
        program_id="xpf",
        workstream_id="deployment",
        entity_refs=("WI:123",),
        text="A stakeholder mentioned a delay.",
        raw_ref=None,
        confidence=Confidence.MEDIUM,
    )


def test_payload_round_trip() -> None:
    signals = (_signal("s1"), _signal("s2"))
    payload = workiq_signals_to_payload(signals)
    restored = workiq_signals_from_payload(payload)
    assert [s.id for s in restored] == ["s1", "s2"]
    assert restored[0].text == signals[0].text


def test_payload_from_payload_handles_missing_signals_key() -> None:
    assert workiq_signals_from_payload({}) == ()


def test_resolve_falls_back_to_live_when_no_snapshot(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    live_called = []

    def live_fetch() -> tuple[Signal, ...]:
        live_called.append(True)
        return (_signal("live-1"),)

    result = resolve_workiq_signals(program_id="xpf", programs_root=programs_root, live_fetch_fn=live_fetch)
    assert live_called == [True]
    assert [s.id for s in result] == ["live-1"]


def test_resolve_prefers_unexpired_committed_snapshot(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    write_prefetch_snapshot(
        program_id="xpf",
        channel=WORKIQ_PREFETCH_CHANNEL,
        payload=workiq_signals_to_payload((_signal("cached-1"),)),
        watermark=None,
        completeness="complete",
        latency_ms=1234.0,
        ttl_seconds=3600,
        programs_root=programs_root,
        now=_NOW,
    )
    live_called = []

    def live_fetch() -> tuple[Signal, ...]:
        live_called.append(True)
        return (_signal("live-1"),)

    result = resolve_workiq_signals(
        program_id="xpf", programs_root=programs_root, live_fetch_fn=live_fetch, now=_NOW
    )
    assert live_called == []  # never called -- snapshot served instead
    assert [s.id for s in result] == ["cached-1"]


def test_resolve_falls_back_when_snapshot_expired(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    write_prefetch_snapshot(
        program_id="xpf",
        channel=WORKIQ_PREFETCH_CHANNEL,
        payload=workiq_signals_to_payload((_signal("cached-1"),)),
        watermark=None,
        completeness="complete",
        latency_ms=1234.0,
        ttl_seconds=1,
        programs_root=programs_root,
        now=_NOW,
    )
    live_called = []

    def live_fetch() -> tuple[Signal, ...]:
        live_called.append(True)
        return (_signal("live-1"),)

    from datetime import timedelta

    # 2 hours after the 1-second TTL snapshot -- definitely expired.
    result = resolve_workiq_signals(
        program_id="xpf", programs_root=programs_root, live_fetch_fn=live_fetch,
        now=_NOW + timedelta(hours=2),
    )
    assert live_called == [True]
    assert [s.id for s in result] == ["live-1"]


def test_resolve_with_none_programs_root_always_falls_back_to_live() -> None:
    live_called = []

    def live_fetch() -> tuple[Signal, ...]:
        live_called.append(True)
        return ()

    resolve_workiq_signals(program_id="xpf", programs_root=None, live_fetch_fn=live_fetch)
    assert live_called == [True]
