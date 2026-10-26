"""Tests for the benchmark-only RELO newsletter/nudge baseline capture."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "relo_trusted_workflow_baseline", REPO_ROOT / "scripts" / "relo_trusted_workflow_baseline.py"
)
baseline = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
sys.modules["relo_trusted_workflow_baseline"] = baseline
_SPEC.loader.exec_module(baseline)


def test_collect_empty_program_is_honest_and_non_identifying(tmp_path: Path) -> None:
    row = baseline.collect_program_benchmark("fixture", programs_root=tmp_path / "programs")
    assert row["program_id"] == "fixture"
    assert row["newsletter"] == {"manual_baseline": None, "trusted_baseline": None}
    assert row["nudge"]["cooldown_state_item_count"] == 0
    assert row["nudge"]["published_eml_artifact_count"] == 0
    assert [metric["confidence"] for metric in row["outcome_metrics"]] == ["unavailable"] * 4


def test_capture_and_verify_round_trip(tmp_path: Path, capsys) -> None:
    programs_root = tmp_path / "programs"
    out_dir = tmp_path / "baselines"
    path = baseline.capture(program_ids=("fixture",), programs_root=programs_root, out_dir=out_dir)
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["classification"] == "benchmark_only"
    assert document["authority"] == "none; operator-selected benchmark inputs only"
    assert document["programs"][0]["program_id"] == "fixture"
    assert baseline.verify(program_ids=("fixture",), programs_root=programs_root, baseline_dir=out_dir) is True
    assert "MATCH" in capsys.readouterr().out


def test_explicit_test_generated_range_excludes_conflicted_trusted_issue() -> None:
    assert baseline._trusted_issue_conflicted_by_notes(
        "Synthetic fixture: test-generated issues 900–910 were moved.", 905
    )
    assert not baseline._trusted_issue_conflicted_by_notes("Synthetic fixture: issue 905 was published.", 905)


def test_verify_reports_drift_without_failing_the_read_only_probe(tmp_path: Path, capsys) -> None:
    programs_root = tmp_path / "programs"
    out_dir = tmp_path / "baselines"
    baseline.capture(program_ids=("fixture",), programs_root=programs_root, out_dir=out_dir)
    (programs_root / "fixture" / "nudge").mkdir(parents=True)
    (programs_root / "fixture" / "nudge" / "nudge_state.json").write_text(
        '{"schema_version":"1.2","item:42":{"triggered_at":"2026-07-28T00:00:00+00:00","origin":"generated"}}',
        encoding="utf-8",
    )
    assert baseline.verify(program_ids=("fixture",), programs_root=programs_root, baseline_dir=out_dir) is True
    assert "DRIFT" in capsys.readouterr().out


def test_outcome_projection_does_not_repair_damaged_ledger(tmp_path: Path) -> None:
    from src.core.ledger.event_log import get_event_logs_dir

    directory = get_event_logs_dir("fixture", programs_root=tmp_path)
    directory.mkdir(parents=True)
    path = directory / "2026-10.events.jsonl"
    path.write_bytes(b'{"broken":')
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}

    rows = baseline._outcomes("fixture", tmp_path)

    assert [row["metric_id"] for row in rows] == [
        "om1_hallucination_rate", "om2_duplicate_entities", "om4_audit_coverage", "om5_operator_friction",
    ]
    assert all(row["value"] is None and row["confidence"] == "unavailable" for row in rows)
    assert all("Could not read" in rows[index]["detail"] for index in (1, 2))
    assert {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("argv", [[], ["--program", "fixture"], ["--program", "fixture", "--programs-root", "unused"]])
def test_cli_requires_explicit_program_root_and_artifact_directory(argv) -> None:
    with pytest.raises(SystemExit) as error:
        baseline.main(argv)
    assert error.value.code == 2


def test_verify_without_captured_artifact_fails_read_only(tmp_path, capsys) -> None:
    assert baseline.verify(program_ids=("fixture",), programs_root=tmp_path / "programs", baseline_dir=tmp_path / "baselines") is False
    assert "no captured artifact" in capsys.readouterr().out
    assert list(tmp_path.iterdir()) == []


def test_empty_program_selection_is_not_a_benchmark(tmp_path) -> None:
    with pytest.raises(ValueError, match="program"):
        baseline.build_artifact(program_ids=(), programs_root=tmp_path)


def test_explicit_cli_capture_and_verify_are_confined_to_selected_roots(tmp_path, capsys) -> None:
    args = [
        "--program", "fixture", "--programs-root", str(tmp_path / "programs"),
        "--baseline-dir", str(tmp_path / "baselines"),
    ]
    assert baseline.main([*args, "--capture"]) == 0
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    assert len(before) == 1

    assert baseline.main([*args, "--verify"]) == 0
    assert "MATCH" in capsys.readouterr().out
    assert {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before
