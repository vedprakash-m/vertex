"""Contract tests for the WS-13/PB-49 flake-bucket dashboard.

Ratchets the flake-tracking surface so it cannot silently regress:
- The sidecar path lives under the canonical program/_state/ directory.
- The state is registered in `state_reader_registry.py` (D-18 contract).
- `record_flake()` is portalocker-routed (PB-37).
- Status transitions (`open` -> `quarantined` -> `fixed`) work end-to-end.
- The `scripts/record_flake.py` runner exists and emits a JSON record.
"""
from __future__ import annotations

import ast
import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FLAKE_BUCKETS_PY = REPO_ROOT / "src" / "core" / "flake_buckets.py"
REGISTRY_PY = REPO_ROOT / "src" / "core" / "state_reader_registry.py"
RECORD_FLAKE_SCRIPT = REPO_ROOT / "scripts" / "record_flake.py"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_flake_buckets_module_exists() -> None:
    assert FLAKE_BUCKETS_PY.exists(), "src/core/flake_buckets.py missing"


def test_flake_buckets_registered_in_state_reader_registry() -> None:
    text = _read(REGISTRY_PY)
    assert "flake_buckets" in text, (
        "flake_buckets state not registered in state_reader_registry.py — "
        "D-18 contract broken"
    )
    # The reader_symbols tuple must export the canonical API
    for sym in ("record_flake", "quarantine_flake", "mark_flake_fixed", "read_flake_buckets"):
        assert sym in text, f"reader_symbols missing {sym!r}"


def test_flake_buckets_path_uses_canonical_layout() -> None:
    """The sidecar MUST live at programs/<program>/_state/flake_buckets.jsonl."""
    text = _read(FLAKE_BUCKETS_PY)
    assert "programs_root / program_id" in text or "programs_root / program" in text, (
        "flake_buckets_path must use programs_root-relative layout (cross-program isolation)"
    )
    assert "_state" in text, (
        "sidecar must live under _state/ (NOT journal/ — flakes are CI-quality, not program events)"
    )
    assert "flake_buckets.jsonl" in text, "sidecar filename convention broken"


def test_flake_buckets_writes_route_through_portalocker() -> None:
    """AST-walk: every `Path.open("a", ...)` in flake_buckets.py is inside `append_jsonl_line` (PB-37)."""
    text = _read(FLAKE_BUCKETS_PY)
    tree = ast.parse(text, filename=str(FLAKE_BUCKETS_PY))
    # We just check that `append_jsonl_line` is imported AND called from this
    # module. The detailed AST-walk is the job of test_concurrency_locking_contract;
    # here we just confirm the import is there and we do NOT use raw `open("a", ...)`.
    assert "append_jsonl_line" in text, (
        "flake_buckets must use append_jsonl_line (PB-37 portalocker contract)"
    )
    # Quick AST sanity: are there ANY direct .open("a",...) calls? (should be 0)
    direct_appends: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "open":
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and "a" in arg.value:
                    direct_appends.append((node.lineno, node.col_offset))
    assert not direct_appends, (
        f"flake_buckets.py has direct .open('a',...) calls at {direct_appends} — "
        "must route through append_jsonl_line (PB-37)"
    )


def test_record_flake_writes_valid_jsonl(tmp_path: Path) -> None:
    """End-to-end: call record_flake, then read_flake_buckets returns the bucket."""
    from src.core.flake_buckets import read_flake_buckets, record_flake

    # First record
    bucket = record_flake(
        "tests/unit/test_foo.py::test_bar",
        program_id="ci-py311-ubuntu",
        programs_root=tmp_path,
        owner="unit-team",
    )
    assert bucket.flake_count == 1
    assert bucket.status.value == "open"
    assert bucket.owner == "unit-team"

    # Read it back
    buckets = read_flake_buckets(program_id="ci-py311-ubuntu", programs_root=tmp_path)
    assert len(buckets) == 1
    assert buckets[0].test_id == "tests/unit/test_foo.py::test_bar"
    assert buckets[0].flake_count == 1


def test_quarantine_flake_transitions_status(tmp_path: Path) -> None:
    """A second-occurrence record + quarantine moves the bucket to quarantined."""
    from src.core.flake_buckets import (
        quarantine_flake,
        read_flake_buckets,
        record_flake,
    )

    record_flake(
        "tests/x.py::test_y",
        program_id="local",
        programs_root=tmp_path,
    )
    record_flake(
        "tests/x.py::test_y",
        program_id="local",
        programs_root=tmp_path,
    )
    bucket = quarantine_flake(
        "tests/x.py::test_y",
        owner="flaky-bucket-team",
        program_id="local",
        programs_root=tmp_path,
        reason="known-ordering bug, fix in PR-1234",
    )
    assert bucket.status.value == "quarantined"
    assert bucket.owner == "flaky-bucket-team"
    assert bucket.flake_count == 2  # preserved from previous record
    assert bucket.suggested_action == "known-ordering bug, fix in PR-1234"

    # Read back
    buckets = read_flake_buckets(program_id="local", programs_root=tmp_path)
    assert len(buckets) == 1
    assert buckets[0].status.value == "quarantined"


def test_mark_flake_fixed_transitions_status(tmp_path: Path) -> None:
    """mark_flake_fixed moves a known-quarantined bucket to fixed."""
    from src.core.flake_buckets import (
        mark_flake_fixed,
        quarantine_flake,
        read_flake_buckets,
    )

    quarantine_flake(
        "tests/z.py::test_w",
        owner="owner",
        program_id="local",
        programs_root=tmp_path,
    )
    fixed = mark_flake_fixed(
        "tests/z.py::test_w",
        program_id="local",
        programs_root=tmp_path,
    )
    assert fixed.status.value == "fixed"

    buckets = read_flake_buckets(program_id="local", programs_root=tmp_path)
    assert buckets[0].status.value == "fixed"


def test_mark_flake_fixed_unknown_raises(tmp_path: Path) -> None:
    """mark_flake_fixed on an unknown test must raise LookupError (fail loud)."""
    from src.core.flake_buckets import mark_flake_fixed

    with pytest.raises(LookupError, match="no flake record"):
        mark_flake_fixed("tests/never_seen.py::test_it", program_id="local", programs_root=tmp_path)


def test_record_flake_script_exists_and_parses() -> None:
    """`scripts/record_flake.py` must exist, be syntactically valid, and import src/."""
    assert RECORD_FLAKE_SCRIPT.exists(), f"{RECORD_FLAKE_SCRIPT} missing"
    text = _read(RECORD_FLAKE_SCRIPT)
    ast.parse(text, filename=str(RECORD_FLAKE_SCRIPT))
    assert "record_flake" in text or "FlakeBucket" in text, (
        "scripts/record_flake.py does not reference the canonical record_flake() helper"
    )


def test_record_flake_script_emits_to_sidecar(tmp_path: Path) -> None:
    """The script must write to the sidecar when given a junitxml with a flaky test."""
    from src.core.flake_buckets import read_flake_buckets

    # Build a tiny junitxml with one flaky testcase
    junit = tmp_path / "junit.xml"
    junit.write_text(
        """<?xml version="1.0"?>
<testsuite>
  <testcase classname="tests.unit.test_x" name="test_flaky_one" time="0.1">
    <flaky message="flaked on retry 2" />
  </testcase>
  <testcase classname="tests.unit.test_x" name="test_stable_one" time="0.05" />
</testsuite>
""",
        encoding="utf-8",
    )

    programs_root = tmp_path / "programs"
    programs_root.mkdir()
    import subprocess

    result = subprocess.run(
        [
            sys.executable if (sys := __import__("sys")) else "python",
            str(RECORD_FLAKE_SCRIPT),
            "--junit",
            str(junit),
            "--program",
            "ci-py311",
            "--programs-root",
            str(programs_root),
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    # Exit 1 is informational (flakes recorded); we just want the sidecar
    assert result.returncode in (0, 1), f"unexpected exit {result.returncode}: {result.stderr}"
    buckets = read_flake_buckets(program_id="ci-py311", programs_root=programs_root)
    assert len(buckets) == 1
    assert "test_flaky_one" in buckets[0].test_id
