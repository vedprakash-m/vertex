"""WS-2 QG-26: ExternalDependency state gate.

QG-26 surfaces the structural state of every `external_dependencies.jsonl`
record to the confirm pipeline. A *critical* dep (criticality="blocker" or
"high") that is NOT `is_fulfilled=True` and NOT `state in {"closed",
"fulfilled", "merged"}` blocks confirm unless waived or forced.

Vacuous state: a program with no `external_dependencies.jsonl` produces a
*passing* QG-26 (the n/a path described in the WS-2 spec). The scorecard
engine treats 0 deps as n/a and readiness must not block.
"""
from __future__ import annotations

from src.core.config_loader import PROGRAMS_ROOT
from src.core.external_dependency import ExternalDependency, load_external_dependencies
from src.core.quality_gates.models import GateEvaluation, QualityGateReport


def _blocking_deps(
    deps: tuple[ExternalDependency, ...],
) -> tuple[ExternalDependency, ...]:
    """Return the subset of deps that should block confirm.

    A dep blocks if it is critical AND not in a terminal state.
    """
    terminal_states = {"closed", "fulfilled", "merged"}
    return tuple(
        dep
        for dep in deps
        if dep.criticality in {"high", "blocker"}
        and dep.state not in terminal_states
        and not dep.is_fulfilled
    )


def evaluate_external_dependency_gate(
    *,
    program_id: str | None,
    programs_root = PROGRAMS_ROOT,
) -> QualityGateReport:
    """WS-2 QG-26: external dependency gate.

    Vacuous path: when no `external_dependencies.jsonl` exists OR no
    blocking deps are present, the gate passes with a sample message.
    """
    if program_id is None:
        return QualityGateReport(results=())
    deps = load_external_dependencies(program_id, programs_root=programs_root)
    blocking = _blocking_deps(deps)
    if not blocking:
        return QualityGateReport(
            results=(
                GateEvaluation(
                    gate_id="QG-26",
                    passed=True,
                    message=(
                        f"External dependency gate passed ({len(deps)} dep(s), 0 blocking)."
                        if deps
                        else "External dependency gate passed (no external_dependencies.jsonl — n/a)."
                    ),
                    exit_code=3,
                    forceable=True,
                ),
            )
        )
    sampled = ", ".join(
        f"{dep.dep_id}:{dep.criticality}:{dep.state}" for dep in blocking[:3]
    )
    if len(blocking) > 3:
        sampled = f"{sampled}; and {len(blocking) - 3} more"
    return QualityGateReport(
        results=(
            GateEvaluation(
                gate_id="QG-26",
                passed=False,
                message=(
                    f"External dependency gate: {len(blocking)} critical dep(s) not in a terminal state ({sampled})."
                ),
                exit_code=3,
                forceable=True,
            ),
        )
    )
