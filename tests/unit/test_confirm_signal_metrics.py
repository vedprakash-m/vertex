"""Direct coverage for the extracted confirm signal-metrics helpers (FR-SG-27).

Guards the D-25 / Phase 3 extraction from ``src/commands/confirm.py`` into
``src/commands/confirm_stages/signal_metrics.py``. Both helpers are pure.
"""

from __future__ import annotations

from types import SimpleNamespace

from src.commands.confirm_stages.signal_metrics import (
    compute_provenance_confidence,
    compute_source_health_pct,
)


def test_source_health_none_when_state_missing() -> None:
    assert compute_source_health_pct(None) is None


def test_source_health_none_when_no_channels() -> None:
    assert compute_source_health_pct(SimpleNamespace(channels=None)) is None
    assert compute_source_health_pct(SimpleNamespace(channels={})) is None


def test_source_health_fraction() -> None:
    state = SimpleNamespace(
        channels={
            "ado": {"active": True, "last_error": None},
            "kusto": {"active": True, "last_error": "boom"},  # has error -> unhealthy
            "teams": {"active": False, "last_error": None},  # inactive -> unhealthy
            "icm": {"active": True},  # healthy
        }
    )
    # 2 healthy of 4 = 0.5
    assert compute_source_health_pct(state) == 0.5


def test_source_health_ignores_non_dict_channel_entries() -> None:
    state = SimpleNamespace(channels={"ado": {"active": True}, "weird": "not-a-dict"})
    # 1 healthy of 2
    assert compute_source_health_pct(state) == 0.5


def test_provenance_confidence_none_when_empty() -> None:
    assert compute_provenance_confidence(()) is None


def test_provenance_confidence_fraction() -> None:
    signals = (
        SimpleNamespace(source_confidence_tier="high"),
        SimpleNamespace(source_confidence_tier="medium"),
        SimpleNamespace(source_confidence_tier="low"),
        SimpleNamespace(),  # missing attr -> defaults to "low"
    )
    # 2 of 4 are high/medium = 0.5
    assert compute_provenance_confidence(signals) == 0.5


def test_provenance_confidence_all_high() -> None:
    signals = (SimpleNamespace(source_confidence_tier="high"),) * 3
    assert compute_provenance_confidence(signals) == 1.0
