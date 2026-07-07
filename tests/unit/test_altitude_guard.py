from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.core.altitude_guard import apply_altitude_guard
from src.core.models import Confidence
from src.core.models_v2 import Signal
from src.core.trajectory_analyzer import DriftPattern


def test_apply_altitude_guard_satellite_strips_low_severity_patterns_and_low_signal_confidence() -> None:
    result = apply_altitude_guard(
        altitude="satellite",
        signals=(
            _signal("keep-high", confidence=Confidence.HIGH, metadata={"severity": "medium"}),
            _signal("drop-low-confidence", confidence=Confidence.LOW),
            _signal("drop-low-severity", confidence=Confidence.HIGH, metadata={"severity": "low"}),
            _signal(
                "keep-risk-escalation",
                confidence=Confidence.LOW,
                metadata={"field": "RiskLevel", "prior": "Medium", "current": "High", "severity": "low"},
            ),
        ),
        drift_patterns=(
            _pattern(101, severity="high"),
            _pattern(202, severity="low"),
        ),
    )

    assert tuple(signal.id for signal in result.signals) == ("keep-high", "keep-risk-escalation")
    assert tuple(pattern.work_item_id for pattern in result.drift_patterns) == (101,)


def test_apply_altitude_guard_helicopter_includes_all_inputs() -> None:
    signals = (
        _signal("high", confidence=Confidence.HIGH),
        _signal("low", confidence=Confidence.LOW, metadata={"severity": "low"}),
    )
    drift_patterns = (
        _pattern(101, severity="high"),
        _pattern(202, severity="low"),
    )

    result = apply_altitude_guard(
        altitude="helicopter",
        signals=signals,
        drift_patterns=drift_patterns,
    )

    assert result.signals == signals
    assert result.drift_patterns == drift_patterns


def test_apply_altitude_guard_street_keeps_low_confidence_signals() -> None:
    low_confidence_signal = _signal("low-confidence", confidence=Confidence.LOW)

    result = apply_altitude_guard(
        altitude="street",
        signals=(low_confidence_signal,),
        drift_patterns=(),
    )

    assert result.signals == (low_confidence_signal,)


def test_apply_altitude_guard_escalation_scopes_to_single_item() -> None:
    result = apply_altitude_guard(
        altitude="escalation",
        escalation_item_id=101,
        signals=(
            _signal("match", entity_refs=("WI:101",)),
            _signal("other", entity_refs=("WI:202",), metadata={"work_item_id": 202}),
        ),
        drift_patterns=(
            _pattern(101, severity="high"),
            _pattern(202, severity="high"),
        ),
    )

    assert tuple(signal.id for signal in result.signals) == ("match",)
    assert tuple(pattern.work_item_id for pattern in result.drift_patterns) == (101,)


def test_apply_altitude_guard_escalation_requires_item_id() -> None:
    with pytest.raises(ValueError, match="escalation_item_id"):
        apply_altitude_guard(
            altitude="escalation",
            signals=(),
            drift_patterns=(),
        )


def _signal(
    signal_id: str,
    *,
    confidence: Confidence = Confidence.HIGH,
    entity_refs: tuple[str, ...] = ("WI:101",),
    metadata: dict[str, str | int | float | bool | None] | None = None,
) -> Signal:
    return Signal(
        id=signal_id,
        timestamp=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
        source="vertex/freshness",
        program_id="acme",
        workstream_id="acme",
        entity_refs=entity_refs,
        text=f"Signal {signal_id}",
        raw_ref=signal_id,
        confidence=confidence,
        metadata=metadata,
    )


def _pattern(work_item_id: int, *, severity: str) -> DriftPattern:
    return DriftPattern(
        work_item_id=work_item_id,
        pattern="eta_drift",
        severity=severity,
        detail=f"Pattern for {work_item_id}",
        occurrences=2,
        window_days=90,
    )