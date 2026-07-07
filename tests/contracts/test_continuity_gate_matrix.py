from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
CONTINUITY_GATES = tuple(f"CG-0{index}" for index in range(1, 10))


def _quality_gates_source() -> str:
    # quality_gates became a package (D-09 split); read all of its modules.
    package_dir = REPO_ROOT / "src/core/quality_gates"
    return "\n".join(sorted(p.read_text(encoding="utf-8") for p in package_dir.glob("*.py")))


def test_all_continuity_gate_ids_exist_in_quality_gate_code() -> None:
    qg_source = _quality_gates_source()

    missing = [gate_id for gate_id in CONTINUITY_GATES if f'"{gate_id}"' not in qg_source]

    assert missing == [], f"Continuity gates missing from quality_gates.py: {missing}"


def test_all_continuity_gate_evaluations_are_registered_in_code() -> None:
    qg_source = _quality_gates_source()
    code_gates = set(re.findall(r'GateEvaluation\("(CG-0[1-9])"', qg_source))

    assert code_gates == set(CONTINUITY_GATES), (
        f"Unexpected continuity gate set in code: expected {CONTINUITY_GATES}, found {sorted(code_gates)}"
    )


def test_all_continuity_gates_have_unit_test_coverage() -> None:
    test_source = (REPO_ROOT / "tests/unit/test_quality_gates.py").read_text(encoding="utf-8")
    untested = [gate_id for gate_id in CONTINUITY_GATES if gate_id not in test_source]

    assert untested == [], f"Continuity gates without unit-test coverage: {untested}"