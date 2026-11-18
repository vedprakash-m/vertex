from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_module():
    path = REPO_ROOT / "scripts" / "derive_test_file_timings.py"
    spec = importlib.util.spec_from_file_location("derive_test_file_timings", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_derives_file_weight_from_junit_classname(tmp_path: Path) -> None:
    module = _load_module()
    junit = tmp_path / "one.xml"
    junit.write_text(
        """<testsuite>
        <testcase classname="tests.unit.test_partition_tests.TestClass" name="test_one" time="1.5"/>
        <testcase classname="tests.unit.test_partition_tests.TestClass" name="test_two" time="0.5"/>
        <testcase classname="tests.unit.test_partition_tests" name="test_three" time="1.0"/>
        </testsuite>""",
        encoding="utf-8",
    )
    assert module.derive_file_timings([junit]) == {"unit/test_partition_tests.py": 3.0}
