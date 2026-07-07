from __future__ import annotations

from datetime import datetime, timezone

from src.core.models import Confidence
from src.core.models_v2 import Signal, SignalClass
from src.core.signal_classification import classify_signal, signal_class


def test_signal_class_prefers_dependency_over_risk_language() -> None:
    signal = Signal(
        id="sig-1",
        timestamp=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
        source="workiq/teams",
        program_id="acme",
        workstream_id="deployment_velocity",
        entity_refs=("WI:1",),
        text="Deployment is at risk because we are blocked by a dependency from another team.",
        raw_ref="raw:1",
        confidence=Confidence.MEDIUM,
        metadata=None,
    )

    assert signal_class(signal) is SignalClass.DEPENDENCY


def test_classify_signal_preserves_existing_signal_class() -> None:
    signal = Signal(
        id="sig-2",
        timestamp=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
        source="workiq/email",
        program_id="acme",
        workstream_id="deployment_velocity",
        entity_refs=("WI:2",),
        text="Just a status note.",
        raw_ref="raw:2",
        confidence=Confidence.MEDIUM,
        metadata={"signal_class": "decision"},
    )

    classified = classify_signal(signal)

    assert classified.metadata is not None
    assert classified.metadata["signal_class"] == SignalClass.DECISION.value
