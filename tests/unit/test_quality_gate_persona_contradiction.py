"""Direct coverage for the extracted persona (QG-P) and contradiction (QG-SG-09) gates.

Guards the D-09 / Phase 3 peels from the ``src/core/quality_gates`` package into
``persona.py`` and ``contradiction.py`` (both re-exported from ``__init__``).
"""

from __future__ import annotations

from types import SimpleNamespace

from src.core.models_v2 import Confidence
from src.core.quality_gates import (
    evaluate_contradiction_gate,
    evaluate_persona_signal_gates,
)


# ── Persona (QG-P) ──────────────────────────────────────────────────────────

def test_persona_none_is_noop() -> None:
    assert evaluate_persona_signal_gates(None).results == ()


def test_persona_not_enforce_is_noop() -> None:
    cov = SimpleNamespace(enforcement_mode="observe", blocks=("x",))
    assert evaluate_persona_signal_gates(cov).results == ()


def test_persona_enforce_no_blocks_passes() -> None:
    cov = SimpleNamespace(enforcement_mode="enforce", blocks=())
    gate = evaluate_persona_signal_gates(cov).results[0]
    assert gate.gate_id == "QG-P" and gate.passed is True and gate.exit_code == 0


def test_persona_enforce_with_blocks_fails() -> None:
    cov = SimpleNamespace(enforcement_mode="enforce", blocks=("a", "b"))
    gate = evaluate_persona_signal_gates(cov).results[0]
    assert gate.passed is False and gate.exit_code == 3
    assert "2 persona signal checks blocking" in gate.message


# ── Contradiction (QG-SG-09) ────────────────────────────────────────────────

def _packet(work_item_id: int, confidence: Confidence, summaries: tuple[str, ...]):
    contradictions = tuple(SimpleNamespace(summary=s) for s in summaries)
    return SimpleNamespace(work_item_id=work_item_id, confidence=confidence, contradictions=contradictions)


def test_contradiction_no_packets_passes() -> None:
    gate = evaluate_contradiction_gate(()).results[0]
    assert gate.gate_id == "QG-SG-09" and gate.passed is True


def test_contradiction_low_confidence_passes() -> None:
    packets = (_packet(1, Confidence.LOW, ("x",)),)
    assert evaluate_contradiction_gate(packets).results[0].passed is True


def test_contradiction_high_confidence_empty_contradictions_passes() -> None:
    packets = (_packet(1, Confidence.HIGH, ()),)
    assert evaluate_contradiction_gate(packets).results[0].passed is True


def test_contradiction_high_confidence_blocks_hard() -> None:
    packets = (_packet(900001, Confidence.HIGH, ("timeline conflict",)),)
    gate = evaluate_contradiction_gate(packets).results[0]
    assert gate.passed is False and gate.exit_code == 3 and gate.forceable is False
    assert "WI:900001" in gate.message
    assert "1 HIGH-confidence contradiction packet(s)" in gate.message


def test_contradiction_samples_first_three_and_counts_overflow() -> None:
    packets = tuple(_packet(i, Confidence.HIGH, (f"c{i}",)) for i in range(5))
    gate = evaluate_contradiction_gate(packets).results[0]
    assert gate.passed is False
    assert "5 HIGH-confidence" in gate.message
    assert "(+2 more)" in gate.message
