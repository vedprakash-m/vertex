from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace


REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_module():
    path = REPO_ROOT / "scripts" / "run_test_partition.py"
    spec = importlib.util.spec_from_file_location("run_test_partition", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_runner_passes_disjoint_paths_directly_to_pytest(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    module = _load_module()
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_one.py").write_text("", encoding="utf-8")
    (tests / "test_two.py").write_text("", encoding="utf-8")
    calls: list[list[str]] = []
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda command, **kwargs: calls.append(command) or SimpleNamespace(returncode=0),
    )
    assert module.main(
        [
            "--tests-root",
            str(tests),
            "--count",
            "1",
            "--index",
            "0",
            "--",
            "-q",
            "-n",
            "2",
        ]
    ) == 0
    assert calls[0][:5] == [sys.executable, "-m", "pytest", "-q", "-n"]
    assert calls[0][5:] == ["2", "tests/test_one.py", "tests/test_two.py"]
    assert "2 test files" in capsys.readouterr().out
