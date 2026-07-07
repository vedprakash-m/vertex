"""Tests for FR-SG-34: adaptive evidence window computation."""
from __future__ import annotations

from datetime import datetime, timezone

from src.core.evidence_assembler import compute_adaptive_signal_window
from src.core.models import Confidence
from src.core.models_v2 import Signal


def _signal(sig_id: str, ts: datetime, workstream_id: str = "ws-1") -> Signal:
    return Signal(
        id=sig_id,
        timestamp=ts,
        source="manual",
        program_id="acme",
        workstream_id=workstream_id,
        entity_refs=(),
        text="test",
        raw_ref=None,
        confidence=Confidence.MEDIUM,
    )


def test_adaptive_window_no_signals_returns_minimum() -> None:
    window = compute_adaptive_signal_window((), workstream_id="ws-1")
    assert window == 7


def test_adaptive_window_single_signal_returns_minimum() -> None:
    signals = (
        _signal("s1", datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc)),
    )
    window = compute_adaptive_signal_window(signals, workstream_id="ws-1")
    assert window == 7


def test_adaptive_window_clamps_to_minimum_when_dense() -> None:
    # 6 signals each 1 day apart → median_interval=1.0, target=3.0, clamp → 7
    base = datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc)
    from datetime import timedelta
    signals = tuple(
        _signal(f"s{i}", base + timedelta(days=i))
        for i in range(6)
    )
    window = compute_adaptive_signal_window(signals, workstream_id="ws-1")
    assert window == 7


def test_adaptive_window_clamps_to_maximum_when_sparse() -> None:
    # 2 signals 20 days apart → median_interval=20.0, target=60.0, clamp → 45
    from datetime import timedelta
    signals = (
        _signal("s1", datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc)),
        _signal("s2", datetime(2026, 5, 21, 0, 0, tzinfo=timezone.utc)),
    )
    window = compute_adaptive_signal_window(signals, workstream_id="ws-1")
    assert window == 45


def test_adaptive_window_computes_median_interval() -> None:
    # 3 signals: gaps of 5 days and 7 days → median=6.0, target=18.0 → 18
    from datetime import timedelta
    t0 = datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc)
    signals = (
        _signal("s1", t0),
        _signal("s2", t0 + timedelta(days=5)),
        _signal("s3", t0 + timedelta(days=12)),
    )
    window = compute_adaptive_signal_window(signals, workstream_id="ws-1")
    assert window == 18


def test_adaptive_window_uses_cadence_when_provided() -> None:
    # cadence_days=10 → target=15.0, clamp → 15
    signals = (
        _signal("s1", datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc)),
    )
    window = compute_adaptive_signal_window(signals, workstream_id="ws-1", cadence_days=10.0)
    assert window == 15


def test_adaptive_window_scoped_to_workstream() -> None:
    # ws-1 has 2 signals 20 days apart (sparse → 45)
    # ws-2 signals ignored
    from datetime import timedelta
    t0 = datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc)
    signals = (
        _signal("s1", t0, workstream_id="ws-1"),
        _signal("s2", t0 + timedelta(days=20), workstream_id="ws-1"),
        _signal("s3", t0 + timedelta(days=1), workstream_id="ws-2"),
        _signal("s4", t0 + timedelta(days=2), workstream_id="ws-2"),
    )
    window = compute_adaptive_signal_window(signals, workstream_id="ws-1")
    assert window == 45
    window_ws2 = compute_adaptive_signal_window(signals, workstream_id="ws-2")
    assert window_ws2 == 7  # ws-2 dense → clamped to 7


def test_adaptive_window_none_workstream_uses_all_signals() -> None:
    # workstream_id=None → all signals used
    from datetime import timedelta
    t0 = datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc)
    signals = (
        _signal("s1", t0, workstream_id="ws-1"),
        _signal("s2", t0 + timedelta(days=15), workstream_id="ws-2"),
    )
    window = compute_adaptive_signal_window(signals, workstream_id=None)
    # gap = 15, target = 45, clamp → 45
    assert window == 45
