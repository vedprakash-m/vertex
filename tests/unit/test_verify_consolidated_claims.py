from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import scripts.verify_consolidated_claims as claims_script


def test_check_git_tracked_passes_when_git_reports_tracked(monkeypatch, tmp_path: Path) -> None:
    spec_path = tmp_path / "specs" / "vertex-prd.md"
    spec_path.parent.mkdir(parents=True)
    spec_path.write_text("spec", encoding="utf-8")

    monkeypatch.setattr(claims_script, "_REPO_ROOT", tmp_path)
    calls: list[list[str]] = []

    def _run(cmd, cwd, check, capture_output, text):
        calls.append(cmd)
        if cmd[:2] == ["git", "ls-files"]:
            return SimpleNamespace(returncode=0)
        if cmd[:2] == ["git", "check-ignore"]:
            return SimpleNamespace(returncode=1)
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(claims_script.subprocess, "run", _run)

    result = claims_script._check_git_tracked(
        claims_script.Claim(
            spec_ref="S-0a",
            description="tracked",
            kind="git_tracked",
            target="specs/vertex-prd.md",
            symbol=None,
        )
    )

    assert result.passed is True
    assert len(calls) == 2


def test_check_git_tracked_fails_when_git_reports_untracked(monkeypatch, tmp_path: Path) -> None:
    spec_path = tmp_path / "specs" / "vertex-prd.md"
    spec_path.parent.mkdir(parents=True)
    spec_path.write_text("spec", encoding="utf-8")

    monkeypatch.setattr(claims_script, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(
        claims_script.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1),
    )

    result = claims_script._check_git_tracked(
        claims_script.Claim(
            spec_ref="S-0a",
            description="tracked",
            kind="git_tracked",
            target="specs/vertex-prd.md",
            symbol=None,
        )
    )

    assert result.passed is False
    assert "not tracked in git" in result.message
