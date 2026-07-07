"""WS-5b: AI budget gate (QG-WS5B).

Evaluates whether the program's AI spending (from ``ai_telemetry.jsonl``) in
the last 24 hours has exceeded the configured ``ai.budget_usd_per_run`` limit
from ``program.yaml``.

Design choices
--------------
* **Forceable** (not hard-blocking) — the cost_guard already enforces a hard
  ceiling mid-run via ``BudgetExceeded``; this gate provides visibility in the
  confirm/report pipeline for the ops team.
* **Window = 24 h** — corresponds roughly to "the most recent gather run".
  A tighter window (per-run) would require the run_id to flow into the gate,
  which would complicate the gate API.  A wider window (all-time) would
  trigger on legitimate cost growth.
* **Vacuous pass** when ``program_id`` is None, no telemetry exists, or
  ``budget_usd_per_run`` is not set (0.0 = disabled, no assertion made).

Note: this gate was originally labelled QG-27 (WS-5b), but QG-27 is reserved
for the truth/conflict gate (WI-3.9, src/core/quality_gates/qg27.py).
Renamed to QG-WS5B to eliminate the ID collision (SD-17).
"""
from __future__ import annotations

from pathlib import Path

from src.core.ai_telemetry import build_feature_cost_summary
from src.core.config_loader import PROGRAMS_ROOT
from src.core.quality_gates.models import GateEvaluation, QualityGateReport

_BUDGET_WINDOW_DAYS = 1  # 24-hour rolling window

# Gate ID is QG-WS5B to avoid collision with the truth/conflict QG-27 (qg27.py).
_GATE_ID = "QG-WS5B"


def evaluate_ai_budget_gate(
    *,
    program_id: str | None,
    budget_usd_per_run: float = 0.0,
    programs_root: Path = PROGRAMS_ROOT,
    window_days: int = _BUDGET_WINDOW_DAYS,
) -> QualityGateReport:
    """QG-WS5B: AI per-run budget gate (forceable).

    ``budget_usd_per_run=0.0`` means no budget is configured; the gate is
    vacuously passing.
    """
    if program_id is None or budget_usd_per_run <= 0.0:
        return QualityGateReport(
            results=(
                GateEvaluation(
                    gate_id=_GATE_ID,
                    passed=True,
                    message="AI budget gate passed (no budget configured — n/a).",
                    exit_code=0,
                    forceable=True,
                ),
            )
        )

    summary = build_feature_cost_summary(
        program_id,
        programs_root=programs_root,
        window_days=window_days,
    )
    if not summary:
        return QualityGateReport(
            results=(
                GateEvaluation(
                    gate_id=_GATE_ID,
                    passed=True,
                    message="AI budget gate passed (no AI telemetry in window — n/a).",
                    exit_code=0,
                    forceable=True,
                ),
            )
        )

    total_cost = sum(s.total_cost_usd for s in summary.values())
    total_calls = sum(s.call_count for s in summary.values())
    total_errors = sum(s.error_count for s in summary.values())

    if total_cost > budget_usd_per_run:
        feature_breakdown = "; ".join(
            f"{f}=${s.total_cost_usd:.3f}/{s.call_count} calls"
            for f, s in sorted(summary.items(), key=lambda kv: -kv[1].total_cost_usd)
        )
        return QualityGateReport(
            results=(
                GateEvaluation(
                    gate_id=_GATE_ID,
                    passed=False,
                    message=(
                        f"AI budget exceeded: ${total_cost:.3f} spent vs "
                        f"${budget_usd_per_run:.2f} budget/run "
                        f"({total_calls} calls, {total_errors} errors). "
                        f"Breakdown: {feature_breakdown}."
                    ),
                    exit_code=3,
                    forceable=True,
                ),
            )
        )

    return QualityGateReport(
        results=(
            GateEvaluation(
                gate_id=_GATE_ID,
                passed=True,
                message=(
                    f"AI budget gate passed: ${total_cost:.3f} of ${budget_usd_per_run:.2f} "
                    f"({total_calls} calls, {total_errors} errors in last {window_days}d)."
                ),
                exit_code=0,
                forceable=True,
            ),
        )
    )
