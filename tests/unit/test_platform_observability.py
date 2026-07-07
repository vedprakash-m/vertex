"""GAP-32: Platform self-observability detectors."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from src.core.platform_observability import (
    detect_ai_safety_drop_rate,
    detect_yield_collapse,
    emit_platform_alerts,
)


def test_yield_collapse_detector_returns_none_when_state_missing(tmp_path: Path) -> None:
    """No gather state → no detection."""
    from src.core.platform_observability import detect_yield_collapse

    result = detect_yield_collapse("acme", programs_root=tmp_path / "programs")
    assert result is None


def test_yield_collapse_detector_returns_none_when_below_threshold(
    tmp_path: Path, monkeypatch
) -> None:
    """Workstreams with 2 or fewer zero-yield cycles are healthy."""
    from src.core import platform_observability

    class _FakeState:
        workstreams = (
            SimpleNamespace(
                workstream_id="ws-1",
                consecutive_zero_signal_cycles=2,
                last_signal_at=datetime.now(timezone.utc),
            ),
        )

    monkeypatch.setattr(
        platform_observability, "load_gather_state", lambda *a, **k: _FakeState
    )

    result = detect_yield_collapse("acme", programs_root=tmp_path / "programs")
    assert result is None


def test_yield_collapse_detector_fires_at_threshold(
    tmp_path: Path, monkeypatch
) -> None:
    """A workstream with 3+ consecutive zero-yield cycles triggers detection."""
    from src.core import platform_observability

    class _FakeState:
        workstreams = (
            SimpleNamespace(
                workstream_id="ws-1",
                consecutive_zero_signal_cycles=3,
                last_signal_at=None,
            ),
            SimpleNamespace(
                workstream_id="ws-2",
                consecutive_zero_signal_cycles=0,
                last_signal_at=datetime.now(timezone.utc),
            ),
        )

    monkeypatch.setattr(
        platform_observability, "load_gather_state", lambda *a, **k: _FakeState
    )

    result = detect_yield_collapse("acme", programs_root=tmp_path / "programs")
    assert result is not None
    assert result.workstream_id == "ws-1"
    assert result.consecutive_zero_cycles == 3


def test_yield_collapse_detector_picks_worst_workstream(
    tmp_path: Path, monkeypatch
) -> None:
    """The detector returns the workstream with the most zero-yield cycles."""
    from src.core import platform_observability

    class _FakeState:
        workstreams = (
            SimpleNamespace(
                workstream_id="ws-1",
                consecutive_zero_signal_cycles=3,
                last_signal_at=None,
            ),
            SimpleNamespace(
                workstream_id="ws-2",
                consecutive_zero_signal_cycles=7,
                last_signal_at=None,
            ),
        )

    monkeypatch.setattr(
        platform_observability, "load_gather_state", lambda *a, **k: _FakeState
    )

    result = detect_yield_collapse("acme", programs_root=tmp_path / "programs")
    assert result is not None
    assert result.workstream_id == "ws-2"
    assert result.consecutive_zero_cycles == 7


def test_ai_safety_drop_detector_returns_none_with_few_samples(
    tmp_path: Path, monkeypatch
) -> None:
    """Need at least 5 recent samples to evaluate drop rate."""
    from src.core import platform_observability

    monkeypatch.setattr(
        platform_observability,
        "read_ai_telemetry",
        lambda *a, **k: tuple(
            SimpleNamespace(status="accepted") for _ in range(3)
        ),
    )
    result = detect_ai_safety_drop_rate(
        "acme", programs_root=tmp_path / "programs"
    )
    assert result is None


def test_ai_safety_drop_detector_returns_none_when_drop_rate_low(
    tmp_path: Path, monkeypatch
) -> None:
    """Drop rate below 50% is healthy."""
    from src.core import platform_observability

    rows = tuple(
        SimpleNamespace(status="accepted" if i % 3 else "ban_dropped")
        for i in range(10)
    )
    monkeypatch.setattr(
        platform_observability,
        "read_ai_telemetry",
        lambda *a, **k: rows,
    )
    result = detect_ai_safety_drop_rate(
        "acme", programs_root=tmp_path / "programs"
    )
    assert result is None


def test_ai_safety_drop_detector_fires_above_threshold(
    tmp_path: Path, monkeypatch
) -> None:
    """Drop rate of 60% (6/10) fires the detector."""
    from src.core import platform_observability

    rows = tuple(
        SimpleNamespace(status="ban_dropped" if i < 6 else "accepted")
        for i in range(10)
    )
    monkeypatch.setattr(
        platform_observability,
        "read_ai_telemetry",
        lambda *a, **k: rows,
    )
    result = detect_ai_safety_drop_rate(
        "acme", programs_root=tmp_path / "programs"
    )
    assert result is not None
    assert result.dropped_generations == 6
    assert result.total_generations == 10
    assert result.drop_rate == 0.6


def test_emit_platform_alerts_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    """Running emit_platform_alerts twice does not append duplicate alerts."""
    from src.core import platform_observability

    class _FakeState:
        workstreams = (
            SimpleNamespace(
                workstream_id="ws-1",
                consecutive_zero_signal_cycles=5,
                last_signal_at=None,
            ),
        )

    monkeypatch.setattr(
        platform_observability, "load_gather_state", lambda *a, **k: _FakeState
    )
    monkeypatch.setattr(
        platform_observability,
        "read_ai_telemetry",
        lambda *a, **k: (),
    )

    first = platform_observability.emit_platform_alerts(
        "acme", programs_root=tmp_path / "programs"
    )
    second = platform_observability.emit_platform_alerts(
        "acme", programs_root=tmp_path / "programs"
    )
    assert len(first) == 1
    assert second == ()
    # Open alerts should still be just the one.
    from src.core.alerts import read_alerts

    open_alerts = read_alerts("acme", programs_root=tmp_path / "programs")
    assert len(open_alerts) == 1
    assert open_alerts[0].category == "platform.yield_collapse"
    assert open_alerts[0].severity == "warn"
