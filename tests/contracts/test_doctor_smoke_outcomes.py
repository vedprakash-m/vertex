from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

from src.core.edition_resolver import resolve_edition_paths


CI_YML = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"
EDITION = "example_tpm_weekly"


def _workflow_python(step_name: str) -> str:
    workflow = yaml.safe_load(CI_YML.read_text(encoding="utf-8"))
    step, = [s for s in workflow["jobs"]["fresh-clone-smoke"]["steps"] if s.get("name") == step_name]
    match = re.search(r"python(?: [^\n]*?)? <<'PY'\n(.*?)\nPY", step["run"], re.DOTALL)
    assert match is not None, "The smoke check must execute testable inline Python."
    return match.group(1)


def _report(status: str = "ok") -> str:
    failed = int(status in {"fail", "error"})
    return json.dumps({
        "edition": EDITION, "overall": "UNHEALTHY" if failed else "HEALTHY",
        "failures": failed, "warnings": int(status == "warn"),
        "checks": [{"label": "Synthetic check", "detail": "Synthetic result", "status": status}],
    })


def _execute_doctor(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, result: subprocess.CompletedProcess[str]) -> Mock:
    monkeypatch.setenv("VERTEX_PROGRAMS_ROOT", str(tmp_path))
    runner = Mock(side_effect=[result, subprocess.CompletedProcess(["vertex", "doctor", "--help"], 0)])
    monkeypatch.setattr(subprocess, "run", runner)
    exec(compile(_workflow_python("vertex doctor against the fresh-clone program"), str(CI_YML), "exec"), {})
    return runner


@pytest.mark.parametrize(("status", "code"), [("ok", 0), ("warn", 0), ("info", 0), ("fail", 1), ("error", 1)])
def test_doctor_smoke_accepts_completed_semantic_results(monkeypatch, tmp_path: Path, status: str, code: int) -> None:
    result = subprocess.CompletedProcess(["vertex"], code, _report(status), "")
    runner = _execute_doctor(monkeypatch, tmp_path, result)
    command = runner.call_args_list[0].args[0]
    assert command == ["vertex", "doctor", "--edition", EDITION, "--format", "json"]
    assert runner.call_args_list[0].kwargs["timeout"] == 600
    assert runner.call_args_list[0].kwargs["capture_output"] is True
    assert runner.call_args_list[1].args[0] == ["vertex", "doctor", "--help"]
    assert runner.call_args_list[1].kwargs["check"] is True
    assert runner.call_args_list[1].kwargs["timeout"] == 600


@pytest.mark.parametrize("code", [-9, 2, 3, 99, 100, 124])
def test_doctor_smoke_rejects_unsupported_exits(monkeypatch, tmp_path: Path, code: int) -> None:
    with pytest.raises(SystemExit):
        _execute_doctor(monkeypatch, tmp_path, subprocess.CompletedProcess(["vertex"], code, _report(), ""))


@pytest.mark.parametrize("code", [0, 1])
@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_doctor_smoke_rejects_tracebacks_even_with_semantic_exit(monkeypatch, tmp_path: Path, code: int, stream: str) -> None:
    output = {"stdout": _report("fail" if code else "ok"), "stderr": ""}
    output[stream] += "\nTraceback (most recent call last):\nRuntimeError: synthetic crash"
    with pytest.raises((SystemExit, ValueError)):
        _execute_doctor(monkeypatch, tmp_path, subprocess.CompletedProcess(["vertex"], code, **output))


@pytest.mark.parametrize("output", ["", "ModuleNotFoundError: missing", "SyntaxError: invalid syntax", "{}", "[]"])
def test_doctor_smoke_rejects_non_reports(monkeypatch, tmp_path: Path, output: str) -> None:
    with pytest.raises((SystemExit, ValueError, KeyError, TypeError)):
        _execute_doctor(monkeypatch, tmp_path, subprocess.CompletedProcess(["vertex"], 1, output, ""))


@pytest.mark.parametrize(
    ("key", "value"),
    [("edition", "wrong_weekly"), ("failures", 0), ("failures", True),
     ("warnings", 1), ("overall", "HEALTHY"), ("checks", []),
     ("checks", [{"status": "unexpected"}])],
)
def test_doctor_smoke_rejects_inconsistent_reports(monkeypatch, tmp_path: Path, key: str, value: object) -> None:
    payload = json.loads(_report("fail"))
    payload[key] = value
    with pytest.raises((SystemExit, ValueError, KeyError, TypeError)):
        _execute_doctor(monkeypatch, tmp_path, subprocess.CompletedProcess(["vertex"], 1, json.dumps(payload), ""))


def test_doctor_smoke_rejects_exit_report_mismatch(monkeypatch, tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        _execute_doctor(monkeypatch, tmp_path, subprocess.CompletedProcess(["vertex"], 0, _report("fail"), ""))


@pytest.mark.parametrize("failure", [subprocess.TimeoutExpired(["vertex"], 600), FileNotFoundError("vertex")])
def test_doctor_smoke_propagates_execution_failure(monkeypatch, tmp_path: Path, failure: Exception) -> None:
    monkeypatch.setenv("VERTEX_PROGRAMS_ROOT", str(tmp_path))
    runner = Mock(side_effect=failure)
    monkeypatch.setattr(subprocess, "run", runner)
    with pytest.raises(type(failure)):
        exec(compile(_workflow_python("vertex doctor against the fresh-clone program"), str(CI_YML), "exec"), {})
    assert runner.call_count == 1


def test_doctor_smoke_rejects_help_failure(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VERTEX_PROGRAMS_ROOT", str(tmp_path))
    runner = Mock(side_effect=[
        subprocess.CompletedProcess(["vertex"], 0, _report(), ""),
        subprocess.CalledProcessError(2, ["vertex", "doctor", "--help"]),
    ])
    monkeypatch.setattr(subprocess, "run", runner)
    with pytest.raises(subprocess.CalledProcessError):
        exec(compile(_workflow_python("vertex doctor against the fresh-clone program"), str(CI_YML), "exec"), {})


def test_doctor_smoke_refuses_implicit_program_root(monkeypatch) -> None:
    monkeypatch.delenv("VERTEX_PROGRAMS_ROOT", raising=False)
    runner = Mock()
    monkeypatch.setattr(subprocess, "run", runner)
    with pytest.raises(SystemExit, match="isolated programs root"):
        exec(compile(_workflow_python("vertex doctor against the fresh-clone program"), str(CI_YML), "exec"), {})
    runner.assert_not_called()


def test_workflow_normalizes_only_isolated_fixture_identity(monkeypatch, tmp_path: Path) -> None:
    program_dir = tmp_path / "freshclone"
    (program_dir / "editions").mkdir(parents=True)
    program_path = program_dir / "program.yaml"
    edition_path = program_dir / "editions" / f"{EDITION}.yaml"
    program_path.write_text("id: example_tpm\nname: Synthetic example\n", encoding="utf-8")
    edition_path.write_text(f"id: {EDITION}\nprogram_id: template:example_tpm\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["-", str(tmp_path)])

    exec(compile(_workflow_python("Set up isolated test program on a temp programs-root"), str(CI_YML), "exec"), {})

    assert yaml.safe_load(program_path.read_text(encoding="utf-8")) == {
        "id": "freshclone", "name": "Synthetic example",
    }
    assert yaml.safe_load(edition_path.read_text(encoding="utf-8")) == {
        "id": EDITION, "program_id": "freshclone",
    }
    resolved = resolve_edition_paths(EDITION, programs_root=tmp_path)
    assert resolved is not None
    assert resolved.program_dir == program_dir
    assert resolved.edition_path == edition_path
    workflow = yaml.safe_load(CI_YML.read_text(encoding="utf-8"))
    step, = [s for s in workflow["jobs"]["fresh-clone-smoke"]["steps"] if s.get("name") == "vertex doctor against the fresh-clone program"]
    assert step["env"]["VERTEX_PROGRAMS_ROOT"] == "${{ env.TMP_PROGRAMS }}"
