"""Direct coverage for the extracted quality-gate models submodule.

Guards the D-09 / Phase 3 first split: the gate result value objects moved to
``src/core/quality_gates/models.py`` and are re-exported from the package
``__init__``. Verifies both import paths resolve to the same objects and the
aggregate-report semantics are preserved.
"""

from __future__ import annotations

from src.core.quality_gates import GateEvaluation as GateEvaluationViaPackage
from src.core.quality_gates import QualityGateReport as ReportViaPackage
from src.core.quality_gates import combine_gate_reports as combine_via_package
from src.core.quality_gates.models import (
    GateEvaluation,
    QualityGateReport,
    combine_gate_reports,
)


def test_reexport_identity() -> None:
    # The package re-export and the submodule must be the same objects.
    assert GateEvaluationViaPackage is GateEvaluation
    assert ReportViaPackage is QualityGateReport
    assert combine_via_package is combine_gate_reports


def test_report_passed_and_exit_code() -> None:
    passing = QualityGateReport(results=(GateEvaluation("QG-1", True, "ok", 0),))
    assert passing.passed is True
    assert passing.exit_code == 0

    failing = QualityGateReport(
        results=(
            GateEvaluation("QG-1", True, "ok", 0),
            GateEvaluation("QG-2", False, "bad", 9),
            GateEvaluation("QG-3", False, "worse", 14),
        )
    )
    assert failing.passed is False
    assert failing.exit_code == 14  # max exit_code among failing


def test_report_qg_results_and_failing_results() -> None:
    report = QualityGateReport(
        results=(
            GateEvaluation("QG-1", True, "ok", 0),
            GateEvaluation("QG-2", False, "bad", 9, forceable=True),
        )
    )
    assert report.qg_results == {"QG-1": True, "QG-2": False}
    failing = report.failing_results
    assert len(failing) == 1 and failing[0].gate_id == "QG-2" and failing[0].forceable is True


def test_combine_gate_reports() -> None:
    a = QualityGateReport(results=(GateEvaluation("QG-1", True, "ok", 0),))
    b = QualityGateReport(results=(GateEvaluation("QG-2", False, "bad", 9),))
    combined = combine_gate_reports(a, b)
    assert [r.gate_id for r in combined.results] == ["QG-1", "QG-2"]
    assert combined.passed is False
    assert combine_gate_reports().results == ()
