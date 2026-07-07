from __future__ import annotations

from pathlib import Path

from src.commands.doctor_checks.checkpoint_checks import checkpoint_issue_number, checkpoint_live_relpaths


def test_checkpoint_issue_number_parses_expected_checkpoint_name() -> None:
    assert checkpoint_issue_number(Path("issue_007_20260530T090000Z")) == 7
    assert checkpoint_issue_number(Path("checkpoint_007")) is None


def test_checkpoint_live_relpaths_reports_only_present_mutable_paths(tmp_path: Path) -> None:
    program_root = tmp_path / "demo"
    (program_root / "journal").mkdir(parents=True)
    (program_root / "chronicle.jsonl").write_text('{"event":"present"}\n', encoding="utf-8")
    (program_root / "journal" / "claims.jsonl").write_text('{"claim":"present"}\n', encoding="utf-8")
    (program_root / "overrides").mkdir(parents=True)

    relpaths = checkpoint_live_relpaths("demo", programs_root=tmp_path)

    assert "chronicle.jsonl" in relpaths
    assert "journal/claims.jsonl" in relpaths
    assert "overrides/" in relpaths
