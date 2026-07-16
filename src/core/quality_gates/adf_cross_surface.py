"""QG-34 Cross-Surface Consistency gate (specs/arch-data-fix.md Section
8.10.9, Section 12.1).

QG-34's "material conflict blocks the affected artifact before write"
enforcement point is not a new evaluator -- it is a relabeled view over the
pre-existing :func:`~src.core.quality_gates.narrative.evaluate_contradiction_narrative_gate`
(QG-17), the same "reconcile with the existing gate rather than duplicate
it" pattern already established for QG-33/QG-WS5B
(:mod:`src.core.quality_gates.adf_economics`). ADF-W2.10 extended QG-17's
underlying `contradiction_engine.py` to also compare the owner field (not
just target_date); this module makes that same coverage visible under the
gate ID Section 12.1's policy matrix actually names for "Cross-Surface
Consistency."
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path

from src.core.models import WorkItem
from src.core.models_v2 import Signal, Workstream
from src.core.quality_gates.models import GateEvaluation
from src.core.quality_gates.narrative import evaluate_contradiction_narrative_gate

GATE_ID = "QG-34"


def evaluate_qg34_cross_surface_consistency_gate(
    *,
    items: tuple[WorkItem, ...],
    approved_signals: tuple[Signal, ...],
    workstream_blurbs: Mapping[str, str] | None,
    narratives: Mapping[str, str] | Iterable[str],
    as_of: datetime,
    program_id: str | None,
    workstreams: tuple[Workstream, ...],
    programs_root: Path,
) -> GateEvaluation:
    """QG-34: delegates entirely to QG-17. Section 8.10.9's six named
    fields (risk, owner, date, milestone status, dependency status, action
    status) are covered exactly as far as `contradiction_engine.py` covers
    them today (target_date, owner -- see ADF-W2.10's status row for the
    other four's explicit deferral)."""
    result = evaluate_contradiction_narrative_gate(
        items=items,
        approved_signals=approved_signals,
        workstream_blurbs=workstream_blurbs,
        narratives=narratives,
        as_of=as_of,
        program_id=program_id,
        workstreams=workstreams,
        programs_root=programs_root,
    )
    return GateEvaluation(
        gate_id=GATE_ID,
        passed=result.passed,
        message=result.message,
        exit_code=result.exit_code,
        forceable=result.forceable,
    )


__all__ = ["GATE_ID", "evaluate_qg34_cross_surface_consistency_gate"]
