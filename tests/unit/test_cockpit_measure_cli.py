"""ADF-W2.11/W3.8/W4.8: `vertex cockpit measure` CLI smoke tests."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from src.commands.cockpit import app
from src.core.proposal_audit import record_proposal_event

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolate_programs_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.cockpit.PROGRAMS_ROOT", programs_root)
    return programs_root


def test_measure_human_format_with_no_data(_isolate_programs_root: Path) -> None:
    result = runner.invoke(app, ["measure", "--program", "xpf"])
    assert result.exit_code == 0
    assert "risk" in result.output
    assert "Total recorded proposal-decision events: 0" in result.output


def test_measure_json_format_is_valid(_isolate_programs_root: Path) -> None:
    result = runner.invoke(app, ["measure", "--program", "xpf", "--format", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["program_id"] == "xpf"
    assert len(payload["by_type"]) == 5


def test_measure_reflects_recorded_events(_isolate_programs_root: Path) -> None:
    record_proposal_event(
        program_id="xpf", proposal_type="risk", proposal_id="r1",
        event="approved", programs_root=_isolate_programs_root,
    )
    result = runner.invoke(app, ["measure", "--program", "xpf", "--format", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    risk_summary = next(s for s in payload["by_type"] if s["proposal_type"] == "risk")
    assert risk_summary["decided_count"] == 1


def test_measure_rejects_unknown_format(_isolate_programs_root: Path) -> None:
    result = runner.invoke(app, ["measure", "--program", "xpf", "--format", "xml"])
    assert result.exit_code != 0
