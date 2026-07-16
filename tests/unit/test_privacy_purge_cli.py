"""ADF-W5.9: `vertex privacy purge` CLI."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

import src.commands.privacy as privacy_module
from src.commands.privacy import privacy_app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _programs_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "programs"
    monkeypatch.setattr(privacy_module, "PROGRAMS_ROOT", root)
    return root


def test_purge_dry_run_by_default_does_not_mutate(_programs_root: Path) -> None:
    old = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
    path = _programs_root / "xpf" / "runtime" / "tier_decisions.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"recorded_at": old}) + "\n", encoding="utf-8")

    result = runner.invoke(privacy_app, ["purge", "--program", "xpf"])
    assert result.exit_code == 0, result.output
    assert "DRY-RUN" in result.output
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1  # unchanged


def test_purge_apply_actually_mutates(_programs_root: Path) -> None:
    old = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
    path = _programs_root / "xpf" / "runtime" / "tier_decisions.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"recorded_at": old}) + "\n", encoding="utf-8")

    result = runner.invoke(privacy_app, ["purge", "--program", "xpf", "--apply"])
    assert result.exit_code == 0, result.output
    assert "APPLIED" in result.output
    assert path.read_text(encoding="utf-8") == ""


def test_purge_json_format_is_valid_json(_programs_root: Path) -> None:
    result = runner.invoke(privacy_app, ["purge", "--program", "xpf", "--format", "json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["program_id"] == "xpf"
    assert payload["dry_run"] is True


def test_purge_rejects_bad_format(_programs_root: Path) -> None:
    result = runner.invoke(privacy_app, ["purge", "--program", "xpf", "--format", "xml"])
    assert result.exit_code != 0
