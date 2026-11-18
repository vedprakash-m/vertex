from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_module():
    path = REPO_ROOT / "scripts" / "partition_tests.py"
    spec = importlib.util.spec_from_file_location("partition_tests", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_partition_files_are_disjoint_and_complete(tmp_path: Path) -> None:
    module = _load_module()
    files = [f"tests/unit/test_{index:02d}.py" for index in range(11)]
    weights = {path: 11 - index for index, path in enumerate(files)}
    shards = module.partition_paths(files, 4, weights)
    flattened = [path for shard in shards for path in shard]
    assert len(flattened) == len(set(flattened)) == len(files)
    assert set(flattened) == set(files)


def test_partition_balances_measured_weights() -> None:
    module = _load_module()
    files = ["a.py", "b.py", "c.py", "d.py", "e.py"]
    weights = {"a.py": 100.0, "b.py": 30.0, "c.py": 28.0, "d.py": 20.0, "e.py": 2.0}
    shards = module.partition_paths(files, 2, weights)
    totals = [sum(weights[path] for path in shard) for shard in shards]
    assert max(totals) - min(totals) <= 20.0
    assert all(shard for shard in shards)


def test_partition_rejects_invalid_index() -> None:
    module = _load_module()
    shards = module.partition_paths(["a.py"], 1, {})
    with pytest.raises(ValueError, match="outside"):
        module.select_partition(shards, 1)


def test_discover_test_files_excludes_non_test_python(tmp_path: Path) -> None:
    module = _load_module()
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_real.py").write_text("", encoding="utf-8")
    (tests / "helper.py").write_text("", encoding="utf-8")
    (tests / "__pycache__").mkdir()
    (tests / "__pycache__" / "test_cached.py").write_text("", encoding="utf-8")
    assert module.discover_test_files(tests) == ["test_real.py"]


def test_cli_supports_nul_delimited_file_lists(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    for index in range(3):
        (tests / f"test_{index}.py").write_text("", encoding="utf-8")
    completed = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "partition_tests.py"),
            "--tests-root",
            str(tests),
            "--count",
            "2",
            "--index",
            "0",
            "--verify",
            "--nul",
        ],
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0
    paths = [item.decode() for item in completed.stdout.split(b"\0") if item]
    assert paths
    assert all(path.endswith(".py") and path.startswith("tests/") for path in paths)


def test_real_checkout_inventory_prefers_git_tracked_files(
    tmp_path: Path, monkeypatch
) -> None:
    module = _load_module()
    tests = tmp_path / "tests"
    tests.mkdir()
    monkeypatch.setattr(module, "DEFAULT_TESTS_ROOT", tests)
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=b"tests/unit/test_one.py\0tests/contracts/test_two.py\0",
        ),
    )
    assert module.discover_test_files(tests) == [
        "contracts/test_two.py",
        "unit/test_one.py",
    ]


def test_functional_inventory_excludes_marker_only_file(tmp_path: Path) -> None:
    module = _load_module()
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_functional.py").write_text(
        "def test_visible():\n    pass\n", encoding="utf-8"
    )
    (tests / "test_performance.py").write_text(
        "import pytest\n@pytest.mark.perf\ndef test_perf():\n    pass\n",
        encoding="utf-8",
    )
    (tests / "test_live.py").write_text(
        "import pytest\npytestmark = pytest.mark.integration\n\ndef test_live():\n    pass\n",
        encoding="utf-8",
    )
    assert module.discover_test_files(tests, functional_only=True) == [
        "test_functional.py"
    ]
