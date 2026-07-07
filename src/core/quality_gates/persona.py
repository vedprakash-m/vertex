"""Persona signal compliance gate (QG-P).

Extracted from the ``src/core/quality_gates`` module (D-09 / Phase 3). Emits a
hard QG-P block when persona coverage is in ``enforce`` mode and any persona
signal check is blocking. Fully self-contained (gate value objects only).
Re-exported from the package ``__init__``.
"""

from __future__ import annotations

from typing import Any

from src.core.quality_gates.models import GateEvaluation, QualityGateReport


def evaluate_persona_signal_gates(persona_coverage: Any | None) -> QualityGateReport:
    if persona_coverage is None or getattr(persona_coverage, "enforcement_mode", None) != "enforce":
        return QualityGateReport(results=())
    blocks = tuple(getattr(persona_coverage, "blocks", ()) or ())
    if not blocks:
        return QualityGateReport(
            results=(
                GateEvaluation(
                    gate_id="QG-P",
                    passed=True,
                    message="QG-P: persona signal checks passed",
                    exit_code=0,
                ),
            )
        )
    return QualityGateReport(
        results=(
            GateEvaluation(
                gate_id="QG-P",
                passed=False,
                message=f"QG-P: {len(blocks)} persona signal checks blocking publication",
                exit_code=3,
            ),
        )
    )
