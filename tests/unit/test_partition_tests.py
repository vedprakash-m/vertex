from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

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
