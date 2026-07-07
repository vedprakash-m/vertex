"""Contract tests for WS-22 learning-loop edit distance trend (coding slice).

Verifies:
  E-1  compute_edit_distance_trend() is exported from src.ai.edit_learner.
  E-2  Returns EditDistanceTrend tuples per task_type.
  E-3  Returns "insufficient_data" when fewer than min_issues issues.
  E-4  "improving" direction when late mean < early mean (fewer edits over time).
  E-5  "declining" direction when late mean > early mean.
  E-6  "flat" direction when early and late means are equal.
  E-7  calibration.py has the edit-distance-trend subcommand.
  E-8  EditDistanceTrend has the expected fields and types.
"""

from __future__ import annotations

import importlib
from datetime import datetime, timezone
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_programs_root(tmp_path: Path, program_id: str = "testprog") -> Path:
    programs_root = tmp_path / "programs"
    (programs_root / program_id / "journal").mkdir(parents=True)
    return programs_root


def _write_edit_patterns(tmp_path: Path, program_id: str, patterns: list[dict]) -> Path:
    """Write a minimal edit_patterns.jsonl fixture."""
    import json

    programs_root = tmp_path / "programs"
    (programs_root / program_id / "journal").mkdir(parents=True, exist_ok=True)
    path = programs_root / program_id / "journal" / "edit_patterns.jsonl"
    lines = [json.dumps(p) for p in patterns]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return programs_root


def _pattern(
    program_id: str,
    issue_number: int,
    override_magnitude: float,
    task_type: str = "exec_summary",
    recorded_at: str = "2025-01-01T00:00:00+00:00",
) -> dict:
    return {
        "program_id": program_id,
        "edition_id": "test_weekly",
        "issue_number": issue_number,
        "section_id": "exec_summary",
        "recorded_at": recorded_at,
        "summary": "test edit",
        "before_excerpt": "before text here",
        "after_excerpt": "after text here",
        "before_word_count": 10,
        "after_word_count": 8,
        "task_type": task_type,
        "author_override_magnitude": override_magnitude,
    }


# ---------------------------------------------------------------------------
# E-1  Module export
# ---------------------------------------------------------------------------


def test_e1_compute_edit_distance_trend_exported() -> None:
    mod = importlib.import_module("src.ai.edit_learner")
    assert hasattr(mod, "compute_edit_distance_trend")
    assert callable(mod.compute_edit_distance_trend)


# ---------------------------------------------------------------------------
# E-2  Returns EditDistanceTrend tuples
# ---------------------------------------------------------------------------


def test_e2_returns_edit_distance_trend_tuples(tmp_path: Path) -> None:
    from src.ai.edit_learner import compute_edit_distance_trend, EditDistanceTrend

    programs_root = _write_edit_patterns(
        tmp_path,
        "testprog",
        [_pattern("testprog", i, 0.5) for i in range(1, 6)],
    )

    trends = compute_edit_distance_trend("testprog", programs_root=programs_root, min_issues_for_trend=4)
    assert isinstance(trends, tuple)
    assert all(isinstance(t, EditDistanceTrend) for t in trends)


# ---------------------------------------------------------------------------
# E-3  insufficient_data when too few issues
# ---------------------------------------------------------------------------


def test_e3_insufficient_data_when_few_issues(tmp_path: Path) -> None:
    from src.ai.edit_learner import compute_edit_distance_trend

    programs_root = _write_edit_patterns(
        tmp_path,
        "testprog",
        [_pattern("testprog", 1, 0.5), _pattern("testprog", 2, 0.4)],
    )

    trends = compute_edit_distance_trend("testprog", programs_root=programs_root, min_issues_for_trend=4)
    assert len(trends) == 1
    assert trends[0].direction == "insufficient_data"
    assert trends[0].mean_override_early is None
    assert trends[0].delta is None


# ---------------------------------------------------------------------------
# E-4  "improving" when late mean < early mean
# ---------------------------------------------------------------------------


def test_e4_improving_direction(tmp_path: Path) -> None:
    from src.ai.edit_learner import compute_edit_distance_trend

    # early issues (1-2) have high override, late issues (3-4) have low override
    patterns = [
        _pattern("testprog", 1, 0.8),
        _pattern("testprog", 2, 0.7),
        _pattern("testprog", 3, 0.2),
        _pattern("testprog", 4, 0.1),
    ]
    programs_root = _write_edit_patterns(tmp_path, "testprog", patterns)

    trends = compute_edit_distance_trend("testprog", programs_root=programs_root, min_issues_for_trend=4)
    assert len(trends) == 1
    trend = trends[0]
    assert trend.direction == "improving"
    assert trend.delta is not None
    assert trend.delta < 0


# ---------------------------------------------------------------------------
# E-5  "declining" when late mean > early mean
# ---------------------------------------------------------------------------


def test_e5_declining_direction(tmp_path: Path) -> None:
    from src.ai.edit_learner import compute_edit_distance_trend

    # early issues have low override, late issues have high override
    patterns = [
        _pattern("testprog", 1, 0.1),
        _pattern("testprog", 2, 0.2),
        _pattern("testprog", 3, 0.8),
        _pattern("testprog", 4, 0.9),
    ]
    programs_root = _write_edit_patterns(tmp_path, "testprog", patterns)

    trends = compute_edit_distance_trend("testprog", programs_root=programs_root, min_issues_for_trend=4)
    assert len(trends) == 1
    trend = trends[0]
    assert trend.direction == "declining"
    assert trend.delta is not None
    assert trend.delta > 0


# ---------------------------------------------------------------------------
# E-6  "flat" when early and late means are equal
# ---------------------------------------------------------------------------


def test_e6_flat_direction(tmp_path: Path) -> None:
    from src.ai.edit_learner import compute_edit_distance_trend

    patterns = [
        _pattern("testprog", 1, 0.5),
        _pattern("testprog", 2, 0.5),
        _pattern("testprog", 3, 0.5),
        _pattern("testprog", 4, 0.5),
    ]
    programs_root = _write_edit_patterns(tmp_path, "testprog", patterns)

    trends = compute_edit_distance_trend("testprog", programs_root=programs_root, min_issues_for_trend=4)
    assert len(trends) == 1
    trend = trends[0]
    assert trend.direction == "flat"
    assert trend.delta == 0.0


# ---------------------------------------------------------------------------
# E-7  calibration.py has the edit-distance-trend subcommand
# ---------------------------------------------------------------------------


def test_e7_calibration_has_edit_distance_trend_command() -> None:
    import ast
    from pathlib import Path

    cal_path = Path("src/commands/calibration.py")
    tree = ast.parse(cal_path.read_text(encoding="utf-8"))

    # Look for @app.command("edit-distance-trend") decorator
    command_decorators = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for deco in node.decorator_list:
                if (
                    isinstance(deco, ast.Call)
                    and isinstance(deco.func, ast.Attribute)
                    and deco.func.attr == "command"
                    and any(
                        isinstance(arg, ast.Constant) and arg.value == "edit-distance-trend"
                        for arg in deco.args
                    )
                ):
                    command_decorators.append(node.name)

    assert command_decorators, (
        "calibration.py must define a @app.command('edit-distance-trend') subcommand"
    )


# ---------------------------------------------------------------------------
# E-8  EditDistanceTrend has expected fields
# ---------------------------------------------------------------------------


def test_e8_edit_distance_trend_fields() -> None:
    from src.ai.edit_learner import EditDistanceTrend
    import dataclasses

    field_names = {f.name for f in dataclasses.fields(EditDistanceTrend)}
    required = {"task_type", "issue_count", "mean_override_early", "mean_override_late", "delta", "direction"}
    assert required.issubset(field_names), f"Missing fields: {required - field_names}"
