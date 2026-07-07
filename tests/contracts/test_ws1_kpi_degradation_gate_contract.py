"""Contract tests for QG-28: KPI degradation gate (WS-1 PB-4).

Tests:
1. Vacuous pass when program_id is None
2. Vacuous pass when no gather state exists for the program
3. Pass when all query states are healthy (is_degraded=False)
4. Forceable failure when ≥1 query is degraded
5. Failure message includes degraded query IDs
6. QG-28 is registered in the QG validation matrix
7. QG-28 is wired into evaluate_phase_1b_gates output
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from src.core.quality_gates import evaluate_phase_1b_gates
from src.core.quality_gates.editorial import evaluate_kpi_degradation_gate
from src.core.models import FreshnessReport

_ = datetime  # keep timezone import used


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _write_gather_state(programs_root: Path, program_id: str, query_states: dict) -> None:
    """Write a minimal gather_state.json for the given program."""
    prog_dir = programs_root / program_id
    prog_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "program_id": program_id,
        "gathered_at": datetime.now(timezone.utc).isoformat(),
        "queries": query_states,
    }
    (prog_dir / "gather_state.json").write_text(json.dumps(payload), encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. Vacuous pass when program_id is None
# ---------------------------------------------------------------------------

def test_qg28_vacuous_pass_no_program_id(tmp_path: Path) -> None:
    result = evaluate_kpi_degradation_gate(program_id=None, programs_root=tmp_path)
    assert result.gate_id == "QG-28"
    assert result.passed is True
    assert result.forceable is True


# ---------------------------------------------------------------------------
# 2. Vacuous pass when gather state absent
# ---------------------------------------------------------------------------

def test_qg28_vacuous_pass_no_gather_state(tmp_path: Path) -> None:
    result = evaluate_kpi_degradation_gate(program_id="prog1", programs_root=tmp_path)
    assert result.gate_id == "QG-28"
    assert result.passed is True


# ---------------------------------------------------------------------------
# 3. Pass when all queries healthy
# ---------------------------------------------------------------------------

def test_qg28_passes_when_all_healthy(tmp_path: Path) -> None:
    _write_gather_state(tmp_path, "prog1", {
        "query-a": {"is_degraded": False, "last_cycle_succeeded": True},
        "query-b": {"is_degraded": False, "last_cycle_succeeded": True},
    })
    result = evaluate_kpi_degradation_gate(program_id="prog1", programs_root=tmp_path)
    assert result.gate_id == "QG-28"
    assert result.passed is True


# ---------------------------------------------------------------------------
# 4. Forceable failure when ≥1 query is degraded
# ---------------------------------------------------------------------------

def test_qg28_fails_forceable_when_degraded(tmp_path: Path) -> None:
    _write_gather_state(tmp_path, "prog1", {
        "acme-deployment-p100": {"is_degraded": True, "last_error": "401 Unauthorized"},
        "acme-health-metrics": {"is_degraded": False},
    })
    result = evaluate_kpi_degradation_gate(program_id="prog1", programs_root=tmp_path)
    assert result.gate_id == "QG-28"
    assert result.passed is False
    assert result.forceable is True
    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# 5. Failure message includes degraded query IDs
# ---------------------------------------------------------------------------

def test_qg28_failure_message_contains_query_id(tmp_path: Path) -> None:
    _write_gather_state(tmp_path, "prog1", {
        "acme-deployment-p100": {"is_degraded": True, "last_error": "401"},
    })
    result = evaluate_kpi_degradation_gate(program_id="prog1", programs_root=tmp_path)
    assert "acme-deployment-p100" in result.message


# ---------------------------------------------------------------------------
# 6. QG-28 registered in validation matrix (smoke: gate_id appears in code)
# ---------------------------------------------------------------------------

def test_qg28_registered_in_validation_matrix() -> None:
    import src.core.quality_gates.editorial as editorial_module
    source = Path(editorial_module.__file__).read_text(encoding="utf-8")
    assert '"QG-28"' in source


# ---------------------------------------------------------------------------
# 7. QG-28 wired into evaluate_phase_1b_gates
# ---------------------------------------------------------------------------

def test_qg28_wired_into_phase_1b_gates(tmp_path: Path) -> None:
    """QG-28 gate_id appears in evaluate_phase_1b_gates output when a query is degraded."""
    _write_gather_state(tmp_path, "pg", {
        "failing-query": {"is_degraded": True, "last_error": "timeout"},
    })
    report = evaluate_phase_1b_gates(
        freshness_report=FreshnessReport(issue_number=1, items=(), blocks=0, warns=0, infos=0),
        program_id="pg",
        programs_root=tmp_path,
    )
    gate_ids = {r.gate_id for r in report.results}
    assert "QG-28" in gate_ids
    qg28 = next(r for r in report.results if r.gate_id == "QG-28")
    assert qg28.passed is False
    assert qg28.forceable is True
