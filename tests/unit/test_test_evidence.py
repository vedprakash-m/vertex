from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_module():
    path = REPO_ROOT / "scripts" / "check_test_evidence.py"
    spec = importlib.util.spec_from_file_location("check_test_evidence", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_junit(path: Path, cases: list[tuple[str, str, str]]) -> None:
    rows = []
    for classname, name, outcome in cases:
        result = "" if outcome == "passed" else f"<{outcome} message=\"synthetic\"/>"
        rows.append(f'<testcase classname="{classname}" name="{name}" time="0.01">{result}</testcase>')
    path.write_text(
        '<?xml version="1.0"?><testsuite tests="{}" failures="0" skipped="0" errors="0">{}</testsuite>'.format(
            len(cases), "".join(rows)
        ),
        encoding="utf-8",
    )


def _coverage(path: Path, executed: tuple[int, ...], missing: tuple[int, ...], covered_branches: tuple[list[int], ...] = ()) -> None:
    payload = {
        "files": {
            "src/example.py": {
                "executed_lines": list(executed),
                "missing_lines": list(missing),
                "excluded_lines": [],
                "executed_branches": list(covered_branches),
                "missing_branches": [],
            }
        },
        "totals": {},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_required_nodes_are_exactly_checked(tmp_path: Path) -> None:
    module = _load_module()
    junit = tmp_path / "one.xml"
    _write_junit(
        junit,
        [
            ("tests.contracts.test_architecture_fitness", "test_inv2_write_confirmed_single_write_path", "passed"),
            ("tests.contracts.test_architecture_fitness", "test_unrelated", "passed"),
        ],
    )
    reports = module.load_junit_reports([junit])
    assert module.missing_required_nodes(
        reports,
        ["tests/contracts/test_architecture_fitness.py::test_inv2_write_confirmed_single_write_path"],
    ) == []
    assert module.missing_required_nodes(
        reports,
        ["tests/contracts/test_architecture_fitness.py::test_inv7_dry_run_never_writes_to_archive"],
    ) == ["tests/contracts/test_architecture_fitness.py::test_inv7_dry_run_never_writes_to_archive"]


def test_coverage_merge_unions_lines_and_branch_arcs(tmp_path: Path) -> None:
    module = _load_module()
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    _coverage(first, (1, 2), (3, 4), ([1, 2],))
    _coverage(second, (2, 3), (1, 4), ([2, 3],))
    result = module.merge_coverage_json([first, second])
    detail = result.files["src/example.py"]
    assert detail.statements == {1, 2, 3, 4}
    assert detail.covered_statements == {1, 2, 3}
    assert detail.covered_branches == {(1, 2), (2, 3)}
    assert result.statement_percent == 75.0


def test_coverage_baseline_requires_exact_source_bytes(tmp_path: Path, monkeypatch) -> None:
    module = _load_module()
    source = tmp_path / "src" / "example.py"
    source.parent.mkdir()
    source.write_text("value = 1\n", encoding="utf-8")
    baseline = tmp_path / "coverage.json"
    baseline.write_text(json.dumps({
        "schema_version": 1,
        "sources": {
            "src/example.py": __import__("hashlib").sha256(
                source.read_bytes()
            ).hexdigest(),
        },
        "files": {
            "src/example.py": {
                "executed_lines": [1],
                "missing_lines": [],
                "executed_branches": [],
                "missing_branches": [],
            },
        },
    }), encoding="utf-8")
    result = module.load_coverage_baseline(
        baseline, tmp_path, expected_files={"src/example.py"},
    )
    assert result.statement_percent == 100.0

    source.write_text("value = 2\n", encoding="utf-8")
    try:
        module.load_coverage_baseline(
            baseline, tmp_path, expected_files={"src/example.py"},
        )
    except ValueError as error:
        assert "source bytes changed" in str(error)
    else:
        raise AssertionError("A stale coverage baseline must fail closed")


def test_private_data_skip_reasons_are_rejected(tmp_path: Path) -> None:
    module = _load_module()
    junit = tmp_path / "one.xml"
    junit.write_text(
        """<testsuite tests="1" failures="0" skipped="1" errors="0">
        <testcase classname="tests.unit.test_example" name="test_private">
          <skipped message="Requires local programs/acme data" />
        </testcase>
        </testsuite>""",
        encoding="utf-8",
    )
    reports = module.load_junit_reports([junit])
    assert module.forbidden_skip_reasons(reports) == [
        "tests.unit.test_example::test_private: Requires local programs/acme data"
    ]


def test_changed_coverage_counts_only_executable_changed_lines(
    tmp_path: Path, monkeypatch
) -> None:
    module = _load_module()
    result = module.CoverageResult(
        {
            "src/example.py": module.FileCoverage(
                statements={1, 2, 3},
                covered_statements={2},
                branches=set(),
                covered_branches=set(),
            )
        }
    )
    monkeypatch.chdir(tmp_path)
    completed = type(
        "Result",
        (),
        {
            "returncode": 0,
            "stdout": "+++ b/src/example.py\n@@ -0,0 +2,2 @@\n+line\n+line\n",
            "stderr": "",
        },
    )()
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: completed)
    assert module.changed_executable_lines("base", ["src"], result) == (1, 2)
