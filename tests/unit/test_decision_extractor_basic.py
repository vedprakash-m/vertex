from __future__ import annotations

from datetime import datetime, timezone

from src.core.decision_extractor_basic import extract_decisions_from_signals
from src.core.models import Confidence
from src.core.models_v2 import DecisionStatus, Signal, SignalClass


def test_extract_decisions_from_signals_builds_proposed_entry_from_strong_decision_signal() -> None:
    signal = Signal(
        id="signal-1",
        timestamp=datetime(2026, 5, 10, 9, 0, tzinfo=timezone.utc),
        source="workiq/transcript",
        program_id="acme",
        workstream_id="deployment",
        entity_refs=("WI:1001",),
        text="LT approved the guarded rollout for WI:1001.",
        raw_ref=None,
        confidence=Confidence.MEDIUM,
        metadata={"signal_class": SignalClass.DECISION.value, "sender_alias": "lt"},
        thread_id=None,
    )

    decisions = extract_decisions_from_signals((signal,), program_id="acme")

    assert len(decisions) == 1
    assert decisions[0].status is DecisionStatus.PROPOSED
    assert decisions[0].decided_by == "lt"
    assert decisions[0].workstream_id == "deployment"
    assert decisions[0].entity_refs == ("WI:1001",)
    assert decisions[0].decision == "LT approved the guarded rollout for WI:1001."
    assert "Derived from workiq/transcript signal signal-1" in decisions[0].context


def test_extract_decisions_from_signals_ignores_unresolved_decision_ask_language() -> None:
    signal = Signal(
        id="signal-2",
        timestamp=datetime(2026, 5, 10, 9, 0, tzinfo=timezone.utc),
        source="workiq/email",
        program_id="acme",
        workstream_id="deployment",
        entity_refs=("WI:1001",),
        text="Leadership ask: need a decision on the rollout path by Friday.",
        raw_ref=None,
        confidence=Confidence.MEDIUM,
        metadata={"signal_class": SignalClass.DECISION.value},
        thread_id=None,
    )

    assert extract_decisions_from_signals((signal,), program_id="acme") == ()
