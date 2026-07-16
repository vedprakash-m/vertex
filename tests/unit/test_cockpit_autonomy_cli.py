"""ADF-W5.12: `vertex cockpit autonomy-evaluate/autonomy-promote/autonomy-demote` CLI."""
from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

import src.commands.cockpit as cockpit_module
from src.commands.cockpit import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _programs_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "programs"
    monkeypatch.setattr(cockpit_module, "PROGRAMS_ROOT", root)
    return root


def test_autonomy_evaluate_all_classes() -> None:
    result = runner.invoke(app, ["autonomy-evaluate", "--program", "xpf"])
    assert result.exit_code == 0, result.output
    assert "risk: promoted l0->l1" in result.output
    assert "meeting_action: promoted l0->l1" in result.output


def test_autonomy_evaluate_single_class() -> None:
    result = runner.invoke(app, ["autonomy-evaluate", "--program", "xpf", "--class", "risk"])
    assert result.exit_code == 0, result.output
    assert "risk: promoted l0->l1" in result.output
    assert "meeting_action" not in result.output


def test_autonomy_evaluate_rejects_unknown_class() -> None:
    result = runner.invoke(app, ["autonomy-evaluate", "--program", "xpf", "--class", "not_a_class"])
    assert result.exit_code != 0


def test_autonomy_promote_explicit() -> None:
    result = runner.invoke(app, [
        "autonomy-promote", "--program", "xpf", "--class", "risk", "--to", "l1", "--reason", "manual test",
    ])
    assert result.exit_code == 0, result.output
    assert "promoted to l1" in result.output


def test_autonomy_promote_rejects_exceeding_ceiling() -> None:
    result = runner.invoke(app, [
        "autonomy-promote", "--program", "xpf", "--class", "risk", "--to", "l4", "--reason", "manual test",
    ])
    assert result.exit_code != 0


def test_autonomy_demote_explicit() -> None:
    runner.invoke(app, ["autonomy-promote", "--program", "xpf", "--class", "risk", "--to", "l1", "--reason", "seed"])
    result = runner.invoke(app, ["autonomy-demote", "--program", "xpf", "--class", "risk", "--reason", "bad output"])
    assert result.exit_code == 0, result.output
    assert "demoted to l0" in result.output
