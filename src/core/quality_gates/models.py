"""Quality-gate result value objects.

Extracted from the ``src/core/quality_gates`` module (D-09 / Phase 3) as the
first leaf cluster of the gate-registry split. These are pure, dependency-free
dataclasses describing a single gate evaluation and an aggregate report. The
package ``__init__`` re-exports them so ``from src.core.quality_gates import
GateEvaluation`` (used throughout the suite) keeps working unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class GateEvaluation:
    gate_id: str
    passed: bool
    message: str
    exit_code: int
    forceable: bool = False


@dataclass(frozen=True, slots=True)
class QualityGateReport:
    results: tuple[GateEvaluation, ...]

    @property
    def passed(self) -> bool:
        return all(result.passed for result in self.results)

    @property
    def exit_code(self) -> int:
        if self.passed:
            return 0
        return max(result.exit_code for result in self.results if not result.passed)

    @property
    def qg_results(self) -> dict[str, bool]:
        return {result.gate_id: result.passed for result in self.results}

    @property
    def failing_results(self) -> tuple[GateEvaluation, ...]:
        return tuple(result for result in self.results if not result.passed)


def combine_gate_reports(*reports: QualityGateReport) -> QualityGateReport:
    return QualityGateReport(results=tuple(result for report in reports for result in report.results))
