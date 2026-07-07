from __future__ import annotations
from pathlib import Path

import pytest

from src.core.readiness_engine import DEFAULT_DIMENSION_ORDER, DEFAULT_READINESS_DIMENSIONS, load_readiness_config


REPO_ROOT = Path(__file__).resolve().parents[2]
PROGRAMS_ROOT = REPO_ROOT / "programs"
# Detect REAL live data, not merely a non-empty programs/ dir: programs/_templates/
# is tracked (rev. 326), so "any(iterdir())" is always True even on the fresh-clone CI.
from tests.support.data_guards import live_program_data_available

_PROGRAMS_EXIST = live_program_data_available()
STANDARD_RD_GATES = tuple(f"QG-RD{index}" for index in range(1, 9))


def test_standard_readiness_gates_are_registered_in_default_order() -> None:
    assert DEFAULT_DIMENSION_ORDER == (
        "slo_definition_complete",
        "dependency_health",
        "observability_coverage",
        "rollback_plan",
        "capacity_validation",
        "incident_response_owner",
        "support_handoff_complete",
        "dora_change_fail_rate",
    )
    assert tuple(gate_id for _, gate_id in DEFAULT_READINESS_DIMENSIONS.values()) == STANDARD_RD_GATES


@pytest.mark.skipif(not _PROGRAMS_EXIST, reason="Requires programs/ data")
def test_nova_readiness_config_covers_standard_readiness_gate_series() -> None:
    config = load_readiness_config("acme", programs_root=PROGRAMS_ROOT)

    assert tuple(dimension.gate_id for dimension in config.dimensions[:8]) == STANDARD_RD_GATES


def test_standard_readiness_gates_have_engine_and_command_test_coverage() -> None:
    combined_tests = "\n".join(
        (
            (REPO_ROOT / "tests/unit/test_readiness_engine.py").read_text(encoding="utf-8"),
            (REPO_ROOT / "tests/unit/test_commands_readiness.py").read_text(encoding="utf-8"),
        )
    )

    untested = [gate_id for gate_id in STANDARD_RD_GATES if gate_id not in combined_tests]

    assert untested == [], f"Readiness gates without unit-test coverage: {untested}"