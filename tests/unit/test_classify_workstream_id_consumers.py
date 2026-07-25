"""Tests for scripts/classify_workstream_id_consumers.py (BL-F2 step 1 tooling)."""

from __future__ import annotations

from pathlib import Path

from scripts.classify_workstream_id_consumers import (
    build_report,
    classify_line,
    find_matches,
    imports_signal,
)


def test_classify_line_detects_assignment() -> None:
    assert classify_line("signal.workstream_id = ws_id") == "assignment"


def test_classify_line_detects_comparison() -> None:
    assert classify_line("if signal.workstream_id != workstream_id:") == "comparison"


def test_classify_line_detects_membership() -> None:
    assert classify_line("if workstream_id in allowed_ids:") == "membership"


def test_classify_line_falls_back_to_attribute_read() -> None:
    assert classify_line("return signal.workstream_id") == "attribute-read"


def test_classify_line_assignment_takes_priority_over_comparison_lookalike() -> None:
    """A walrus-adjacent or chained line could plausibly match both an
    assignment and a comparison pattern; assignment must win since it is the
    more actionable signal for BL-F2's steps (2)-(4) triage."""
    assert classify_line("obj.workstream_id = other.workstream_id") == "assignment"


def _write(root: Path, relative: str, content: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_find_matches_scans_only_files_with_hits(tmp_path: Path) -> None:
    src = tmp_path / "src"
    _write(
        src,
        "core/has_match.py",
        "def f(signal):\n    return signal.workstream_id\n",
    )
    _write(
        src,
        "core/no_match.py",
        "def g():\n    return 1\n",
    )
    matches = find_matches(src_root=src, repo_root=tmp_path)
    files = {m.file for m in matches}
    assert files == {"src/core/has_match.py"}


def test_find_matches_reports_correct_line_numbers_and_patterns(tmp_path: Path) -> None:
    src = tmp_path / "src"
    _write(
        src,
        "core/multi.py",
        "\n".join(
            [
                "def f(signal, workstream_id):",
                "    if signal.workstream_id != workstream_id:",
                "        return None",
                "    signal.workstream_id = workstream_id",
                "    return signal.workstream_id",
                "",
            ]
        ),
    )
    matches = find_matches(src_root=src, repo_root=tmp_path)
    by_line = {m.line_no: m.pattern for m in matches}
    assert by_line == {2: "comparison", 4: "assignment", 5: "attribute-read"}


def test_imports_signal_true_when_import_present(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "core/consumer.py",
        "from src.core.models_v2 import Signal\n\ndef f(s: Signal) -> None:\n    return None\n",
    )
    assert imports_signal(path) is True


def test_imports_signal_false_when_absent(tmp_path: Path) -> None:
    path = _write(tmp_path, "core/other.py", "def f() -> None:\n    return None\n")
    assert imports_signal(path) is False


def test_imports_signal_false_for_missing_file(tmp_path: Path) -> None:
    assert imports_signal(tmp_path / "does_not_exist.py") is False


def test_build_report_aggregates_across_files(tmp_path: Path) -> None:
    src = tmp_path / "src"
    _write(
        src,
        "core/a.py",
        "from src.core.models_v2 import Signal\n\ndef f(signal: Signal):\n    return signal.workstream_id\n",
    )
    _write(
        src,
        "core/b.py",
        "def g(signal):\n    if signal.workstream_id in known_ids:\n        pass\n",
    )
    report = build_report(src_root=src, repo_root=tmp_path)

    assert report["total_files_with_matches"] == 2
    assert report["total_matches"] == 2
    assert report["signal_importing_file_count"] == 1
    assert report["pattern_counts"] == {"attribute-read": 1, "membership": 1}

    files_by_name = {f["file"]: f for f in report["files"]}
    assert files_by_name["src/core/a.py"]["imports_signal"] is True
    assert files_by_name["src/core/b.py"]["imports_signal"] is False


def test_build_report_against_real_repo_matches_bl_f2_reconnaissance_counts() -> None:
    """Adversarial/regression guard, updated 2026-07-25 after BL-F2's plural
    Signal.workstream_ids implementation converted 12 of the original 42
    comparison/assignment sites from `==`/`!=` to `in`/`not in` (now counted
    as `membership`, not `comparison`) and moved 2 files' worth of matches to
    only reference `.workstream_ids` (the new plural field, not matched by
    this script's `.workstream_id\\b` pattern). Real counts today: 117 files,
    40 also importing `Signal`. If it drifts further, either the codebase
    changed again (expected, and this test should be updated alongside a
    fresh reconnaissance note in specs/bklg.md's BL-F2 row) or the script's
    classification logic regressed (a real bug)."""
    report = build_report()
    assert report["total_files_with_matches"] == 117
    assert report["signal_importing_file_count"] == 40
