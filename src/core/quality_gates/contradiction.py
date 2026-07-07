"""Contradiction quality gate (QG-SG-09).

Extracted from the ``src/core/quality_gates`` module (D-09 / Phase 3). Hard-fails
publication when any HIGH-confidence contradiction packet exists (FR-SG-35).
Self-contained: depends only on the gate value objects and the contradiction
domain types. Re-exported from the package ``__init__``.
"""

from __future__ import annotations

from src.core.models_v2 import Confidence, ContradictionPacket
from src.core.quality_gates.models import GateEvaluation, QualityGateReport


def evaluate_contradiction_gate(
    packets: tuple[ContradictionPacket, ...] | list[ContradictionPacket],
) -> QualityGateReport:
    """QG-SG-09: fail if any HIGH-confidence contradiction packet exists (FR-SG-35)."""
    high_confidence_packets = [
        p for p in packets
        if p.confidence == Confidence.HIGH and p.contradictions
    ]
    if not high_confidence_packets:
        return QualityGateReport(results=(GateEvaluation(
            gate_id="QG-SG-09",
            passed=True,
            message="No HIGH-confidence contradictions detected.",
            exit_code=0,
        ),))
    sample = high_confidence_packets[:3]
    details = "; ".join(
        f"WI:{p.work_item_id} ({p.contradictions[0].summary[:60]})"
        for p in sample
    )
    count = len(high_confidence_packets)
    suffix = f" (+{count - len(sample)} more)" if count > len(sample) else ""
    return QualityGateReport(results=(GateEvaluation(
        gate_id="QG-SG-09",
        passed=False,
        message=(
            f"QG-SG-09: {count} HIGH-confidence contradiction packet(s) detected — "
            f"resolve before publishing. {details}{suffix}"
        ),
        exit_code=3,
        forceable=False,
    ),))
