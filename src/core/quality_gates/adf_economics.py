"""QG-33 AI Economics gate (ADF-W0.9, specs/arch-data-fix.md Section 12.1).

The audit reconciliation (Section 2) rejected a duplicate economics gate:
"Reconcile QG-33 with existing QG-WS5B/cost guard rather than duplicate it."
QG-33 is therefore not a new evaluator — it is a relabeled view over the
pre-existing :func:`~src.core.quality_gates.ai_budget.evaluate_ai_budget_gate`
(QG-WS5B) so there is exactly one economics enforcement path. See
:data:`src.core.quality_gates.gate_registry.QG_POLICY_MATRIX` for the full
Section 12.1 policy row (enforcement point, forceability, activation).
"""

from __future__ import annotations

from pathlib import Path

from src.core.quality_gates.ai_budget import evaluate_ai_budget_gate
from src.core.quality_gates.models import GateEvaluation, QualityGateReport

GATE_ID = "QG-33"


def evaluate_qg33_ai_economics_gate(
    *,
    program_id: str | None,
    budget_usd_per_run: float = 0.0,
    programs_root: Path,
) -> QualityGateReport:
    """QG-33 AI Economics: delegates entirely to QG-WS5B.

    Spend-ceiling behavior (forceable=False) matches QG-WS5B unchanged;
    frontier-avoidance-miss reporting (n/a until certification, Section
    12.1) is advisory-only in Phase 0 and does not add a second block path.
    """
    report = evaluate_ai_budget_gate(
        program_id=program_id,
        budget_usd_per_run=budget_usd_per_run,
        programs_root=programs_root,
    )
    relabeled = tuple(
        GateEvaluation(
            gate_id=GATE_ID,
            passed=result.passed,
            message=result.message,
            exit_code=result.exit_code,
            forceable=result.forceable,
        )
        for result in report.results
    )
    return QualityGateReport(results=relabeled)


__all__ = ["GATE_ID", "evaluate_qg33_ai_economics_gate"]
