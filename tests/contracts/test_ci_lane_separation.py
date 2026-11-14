"""RELO-W0.7a: the bounded performance lane is opt-in."""

from __future__ import annotations

import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest
import yaml


@pytest.mark.perf
def test_perf_lane_marker_is_registered() -> None:
    """A deliberately tiny sentinel keeps explicit ``-m perf`` collection non-empty."""
    assert True


def test_chart_budget_runs_serially_in_ci() -> None:
    workflow_path = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["validate"]["steps"]
    functional_index = next(i for i, step in enumerate(steps) if step.get("name") == "Run full test suite")
    budget_steps = [(i, step) for i, step in enumerate(steps) if step.get("name") == "Run chart performance budget"]

    assert len(budget_steps) == 1
    index, step = budget_steps[0]
    assert index > functional_index
    assert step.get("timeout-minutes") == 3
    assert step.get("continue-on-error", False) is False
    assert "if" not in step
    assert shlex.split(step["run"]) == [
        "python", "-m", "pytest", "-n", "0", "-m", "perf",
        "tests/unit/test_declarative_chart_builder.py::test_render_under_2_seconds", "-q",
    ]


@pytest.mark.parametrize(
    ("name", "timeout", "node_id"),
    [
        ("Run ledger replay performance budget", 10, "tests/unit/test_ledger_perf.py::test_replay_perf_50k"),
        ("Run structural gap performance budgets", 3, "tests/contracts/test_structural_gap_rules.py::TestStructuralGapPerf"),
        ("Run pilot replay throughput budgets", 3, "tests/contracts/test_s11c_load_soak.py::TestReplayThroughput"),
        ("Run persona evaluation performance budget", 3, "tests/unit/test_persona_checker.py::test_full_registry_evaluation_under_500ms"),
    ],
)
def test_named_performance_budgets_run_serially_in_ci(name: str, timeout: int, node_id: str) -> None:
    workflow_path = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["validate"]["steps"]
    functional_index = next(i for i, step in enumerate(steps) if step.get("name") == "Run full test suite")
    budget_steps = [(i, step) for i, step in enumerate(steps) if step.get("name") == name]

    assert len(budget_steps) == 1
    index, step = budget_steps[0]
    assert index > functional_index
    assert step.get("timeout-minutes") == timeout
    assert step.get("continue-on-error", False) is False
    assert "if" not in step
    assert shlex.split(step["run"]) == [
        "python", "-m", "pytest", "-n", "0", "-m", "perf", node_id, "-q",
    ]


def test_gather_baseline_runs_serially_outside_checkout_in_ci(tmp_path: Path) -> None:
    workflow_path = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["validate"]["steps"]
    functional_index = next(i for i, step in enumerate(steps) if step.get("name") == "Run full test suite")
    budget_steps = [(i, step) for i, step in enumerate(steps) if step.get("name") == "Run gather performance baseline"]

    assert len(budget_steps) == 1
    index, step = budget_steps[0]
    assert index > functional_index
    assert step.get("timeout-minutes") == 3
    assert step.get("continue-on-error", False) is False
    assert "if" not in step
    assert step["working-directory"] == "${{ runner.temp }}"
    assert shlex.split(step["run"]) == [
        "python", "-m", "pytest", "-n", "0", "-m", "perf",
        "${{ github.workspace }}/tests/perf/test_perf_baseline.py::test_perf_baseline_50_item_fixture", "-q",
    ]
    env = os.environ.copy()
    env["PYTHONPATH"] = step.get("env", {}).get("PYTHONPATH", "").replace(
        "${{ github.workspace }}", str(workflow_path.parents[2])
    )
    result = subprocess.run(
        [sys.executable, "-c", "from tests.support.data_guards import live_program_data_available"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
