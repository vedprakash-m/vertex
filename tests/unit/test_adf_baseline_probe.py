"""Unit tests for ADF-W0.3 baseline probe runner (scripts/adf_baseline.py)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location("adf_baseline", REPO_ROOT / "scripts" / "adf_baseline.py")
adf_baseline = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
sys.modules["adf_baseline"] = adf_baseline
_SPEC.loader.exec_module(adf_baseline)


def test_build_probes_never_raises_on_empty_fixture_program(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    rows = adf_baseline.build_probes(program_id="fixture_prog", programs_root=programs_root)
    assert rows
    for row in rows:
        assert row.status in {"ok", "accepted_limitation", "error"}
        # No probe should error out just because the fixture program has no
        # data yet -- an absent program directory is the CI/fresh-clone case.
        assert row.status != "error", f"{row.evidence}: {row.detail}"


def test_build_probes_marks_unautomated_rows_with_owning_work_item(tmp_path: Path) -> None:
    rows = adf_baseline.build_probes(program_id="fixture_prog", programs_root=tmp_path / "programs")
    limitations = [row for row in rows if row.status == "accepted_limitation"]
    assert limitations
    for row in limitations:
        assert row.owner, f"{row.evidence} has no owning work item"
        assert row.detail


def test_capture_writes_versioned_artifact(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    out_dir = tmp_path / "baselines"
    path = adf_baseline.capture(program_id="fixture_prog", programs_root=programs_root, out_dir=out_dir)
    assert path.exists()
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["schema_version"] == adf_baseline.ARTIFACT_SCHEMA_VERSION
    assert document["program_id"] == "fixture_prog"
    assert document["git_sha"]
    assert document["rows"]
    for row in document["rows"]:
        assert set(row.keys()) == {"evidence", "command", "owner", "status", "value", "detail"}


def test_verify_reproduces_capture_without_drift(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    out_dir = tmp_path / "baselines"
    adf_baseline.capture(program_id="fixture_prog", programs_root=programs_root, out_dir=out_dir)
    reproducible = adf_baseline.verify(program_id="fixture_prog", programs_root=programs_root, baseline_dir=out_dir)
    assert reproducible is True


def test_verify_reports_false_on_probe_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _broken(*, program_id: str, programs_root: Path) -> tuple:
        return (adf_baseline._run_probe(evidence="broken", command="boom", owner="test", fn=_raise),)

    def _raise() -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(adf_baseline, "build_probes", _broken)
    reproducible = adf_baseline.verify(program_id="fixture_prog", programs_root=tmp_path / "programs", baseline_dir=tmp_path / "baselines")
    assert reproducible is False


def test_main_capture_and_verify_cli(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    programs_root = tmp_path / "programs"
    monkey_baseline_dir = tmp_path / "baselines"

    original_dir = adf_baseline.BASELINE_DIR
    try:
        adf_baseline.BASELINE_DIR = monkey_baseline_dir
        exit_code = adf_baseline.main(["--capture", "--program", "fixture_prog", "--programs-root", str(programs_root)])
        assert exit_code == 0
        exit_code = adf_baseline.main(["--verify", "--program", "fixture_prog", "--programs-root", str(programs_root)])
        assert exit_code == 0
    finally:
        adf_baseline.BASELINE_DIR = original_dir
