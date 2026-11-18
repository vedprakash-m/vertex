from __future__ import annotations

import json
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = REPO_ROOT / "governance" / "test-policy.json"
CI_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"


def _policy() -> dict:
    return json.loads(POLICY_PATH.read_text(encoding="utf-8"))


def _workflow() -> dict:
    return yaml.safe_load(CI_PATH.read_text(encoding="utf-8"))


def test_pytest_defaults_do_not_start_implicit_xdist_workers() -> None:
    pyproject = PYPROJECT_PATH.read_text(encoding="utf-8")
    assert """addopts = "-m 'not perf'\"""" in pyproject
    assert "-n auto" not in pyproject


def test_policy_declares_exact_lane_contracts() -> None:
    policy = _policy()
    assert policy["functional_selection"] == "not perf and not integration and not operator"
    assert policy["functional_shards"] == 10
    assert policy["workers_per_shard"] == 4
    assert len(policy["performance_nodes"]) == 8
    assert len(set(policy["performance_nodes"])) == 8
    assert policy["coverage"]["statement_target"] == 95.0
    assert policy["coverage"]["changed_code_statement_floor"] == 95.0
    assert policy["coverage"]["branch_floor"] > 0


def test_workflow_has_fail_closed_parallel_lanes_and_aggregate() -> None:
    workflow = _workflow()
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"]["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"

    jobs = workflow["jobs"]
    assert jobs["functional"]["strategy"]["matrix"]["shard"] == list(range(10))
    assert jobs["guard"]["timeout-minutes"] <= 5
    assert jobs["fresh-clone-smoke"]["timeout-minutes"] <= 12
    assert jobs["windows-smoke"]["runs-on"] == "windows-latest"
    assert jobs["vertex-required"]["if"] == "always()"
    assert set(jobs["vertex-required"]["needs"]) == {
        "guard",
        "functional",
        "fresh-clone-smoke",
        "windows-smoke",
    }


def test_workflow_actions_are_pinned_to_full_commit_shas() -> None:
    action_files = [
        *sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml")),
        *sorted((REPO_ROOT / ".github" / "actions").rglob("action.yml")),
    ]
    lines = [
        line
        for path in action_files
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("- uses:"):
            continue
        reference = stripped.removeprefix("- uses:").strip().rsplit("@", 1)[-1]
        if reference.startswith("./"):
            continue
        assert len(reference) == 40
        int(reference, 16)


def test_duplicate_contract_and_golden_steps_are_removed() -> None:
    workflow = _workflow()
    commands = [
        step.get("run", "")
        for job in workflow["jobs"].values()
        for step in job.get("steps", ())
    ]
    assert not any("pytest tests/contracts/" in command for command in commands)
    assert not any("test_report_pipeline_snapshots.py" in command for command in commands)
    assert not any("check_coverage_baseline.py" in command for command in commands)


def test_backlog_workflow_is_folded_into_required_gate() -> None:
    workflow = _workflow()
    guard_commands = [
        step.get("run", "")
        for step in workflow["jobs"]["guard"].get("steps", ())
    ]
    assert not (REPO_ROOT / ".github" / "workflows" / "backlog-protection.yml").exists()
    assert any("check_backlog_citations.py" in command for command in guard_commands)
    assert any("check_spec_bulk_deletion.py" in command for command in guard_commands)


def test_policy_covers_each_required_critical_obligation() -> None:
    policy = _policy()
    required_ids = {
        "QG-4",
        "QG-5",
        "QG-6",
        "QG-8",
        "QG-11",
        "QG-13",
        "QG-21",
        "QG-22",
        "QG-27",
        "QG-SG-20",
        "QG-DM-1",
        "QG-DM-2",
        "QG-DM-4",
        "QG-DM-8",
        "QG-DM-11",
        "QG-DM-12",
        "QG-29",
        "INV-2",
        "INV-7",
        "ZONE-BOUNDARY",
    }
    assert {entry["id"] for entry in policy["critical_obligations"]} == required_ids
    assert all(entry["node"] for entry in policy["critical_obligations"])


def test_workflow_honors_measured_sharding_and_evidence_policy() -> None:
    policy = _policy()
    workflow = _workflow()
    functional_steps = workflow["jobs"]["functional"]["steps"]
    run_step = next(
        step for step in functional_steps if step.get("name") == "Run functional shard with coverage"
    )
    command = run_step["run"]
    assert f"--count {policy['functional_shards']}" in command
    assert "--weights governance/test-file-timings.json" in command
    assert f"-n {policy['workers_per_shard']}" in command
    assert "--dist worksteal" in command
    assert "--cov=src --cov=cli --cov-branch" in command
    assert "--strict-markers" in command


def test_scale_and_compatibility_have_non_pr_qualification() -> None:
    scale_path = REPO_ROOT / ".github" / "workflows" / "scale.yml"
    assert scale_path.exists()
    scale = yaml.safe_load(scale_path.read_text(encoding="utf-8"))
    events = scale.get("on") or scale.get(True)
    assert events["schedule"]
    assert scale["jobs"]["registry-scale"]["timeout-minutes"] == 30
    assert scale["jobs"]["macos-smoke"]["runs-on"] == "macos-latest"
