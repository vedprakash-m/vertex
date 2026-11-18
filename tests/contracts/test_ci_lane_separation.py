"""RELO-W0.7a: the bounded performance lane is opt-in."""

from __future__ import annotations

import os
from pathlib import Path
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
    job = workflow["jobs"]["guard"]
    assert job["runs-on"] == "ubuntu-latest"
    assert job["timeout-minutes"] == 5
    command = next(
        step["run"]
        for step in job["steps"]
        if step.get("name") == "Run seven bounded performance nodes"
    )
    assert "tests/unit/test_declarative_chart_builder.py::test_render_under_2_seconds" in command
    assert "-n 0 -m perf" in command


@pytest.mark.parametrize(
    "node_id",
    [
        "tests/unit/test_ledger_perf.py::test_replay_perf_50k",
        "tests/contracts/test_structural_gap_rules.py::TestStructuralGapPerf::test_gap_evaluation_under_100ms_for_1000_facts",
        "tests/contracts/test_structural_gap_rules.py::TestStructuralGapPerf::test_gap_evaluation_with_all_rules_firing",
        "tests/contracts/test_s11c_load_soak.py::TestReplayThroughput::test_replay_throughput_meets_slo",
        "tests/contracts/test_s11c_load_soak.py::TestReplayThroughput::test_incremental_replay_is_at_least_as_fast_as_full_rebuild",
        "tests/unit/test_persona_checker.py::test_full_registry_evaluation_under_500ms",
    ],
)
def test_named_performance_budgets_run_serially_in_ci(node_id: str) -> None:
    workflow_path = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    command = next(
        step["run"]
        for step in workflow["jobs"]["guard"]["steps"]
        if step.get("name") == "Run seven bounded performance nodes"
    )
    assert node_id in command
    assert "-n 0 -m perf" in command


def test_gather_baseline_runs_serially_outside_checkout_in_ci(tmp_path: Path) -> None:
    workflow_path = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    step, = [
        step
        for step in workflow["jobs"]["guard"]["steps"]
        if step.get("name") == "Run gather performance baseline"
    ]
    assert step["working-directory"] == "${{ runner.temp }}"
    assert "tests/perf/test_perf_baseline.py::test_perf_baseline_50_item_fixture" in step["run"]
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
