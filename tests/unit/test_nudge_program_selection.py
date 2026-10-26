from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import Mock

import pytest
import typer
from typer.testing import CliRunner

from src.commands import nudge
from src.core.edition_resolver import get_nudge_paths
from src.core.exceptions import ConfigError


INVALID_PROGRAMS = (
    "", " ", ".", "..", "../escape", r"..\escape", "nested/program",
    r"nested\program", r"Q:\outside", r"Q:outside", "*", "program?",
    "[program]", "program.", "program ", "program\n",
)


@pytest.mark.parametrize("program", INVALID_PROGRAMS)
def test_nudge_paths_reject_non_bare_programs(tmp_path: Path, program: str) -> None:
    with pytest.raises(ConfigError, match="bare program identifier"):
        get_nudge_paths(program, programs_root=tmp_path)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("program", ["fixture", "fixture-two", "fixture_two", "fixture.v2"])
def test_nudge_paths_preserve_valid_programs_without_io(tmp_path: Path, program: str) -> None:
    paths = get_nudge_paths(program, programs_root=tmp_path)
    assert paths.nudge_root == tmp_path / program / "nudge"
    assert paths.drafts_dir == paths.nudge_root / "drafts"
    assert paths.state_path == paths.nudge_root / "nudge_state.json"
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("program", INVALID_PROGRAMS)
def test_nudge_generation_rejects_selector_before_context_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, program: str,
) -> None:
    context = Mock(side_effect=AssertionError("No context access for an invalid selector"))
    monkeypatch.setattr(nudge.gather_helpers, "_load_program_context", context)
    with pytest.raises(ConfigError, match="bare program identifier"):
        nudge.generate_full_hygiene_nudges(
            program_id=program, dry_run=True, programs_root=tmp_path,
        )
    context.assert_not_called()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("mode", [
    ["--dry-run"], ["--audit-registry"], ["--list-drafts"],
    ["--approve-draft", "fixture"], ["--mark-sent", "fixture"],
    ["--import-sent", "fixture"], ["--reset-cooldown"],
    ["--reset-cooldown", "--yes"],
])
@pytest.mark.parametrize("program", ["../escape", r"Q:\outside", "program\n"])
def test_nudge_modes_report_invalid_selectors_before_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: list[str], program: str,
) -> None:
    monkeypatch.setattr(nudge, "PROGRAMS_ROOT", tmp_path)
    forbidden = Mock(side_effect=AssertionError("No dispatch for an invalid selector"))
    for name in (
        "generate_full_hygiene_nudges", "_run_registry_audit", "_cmd_list_drafts",
        "_cmd_approve_draft", "_cmd_mark_sent", "_cmd_import_sent",
        "_nudge_read_path", "reset_nudge_item_state",
    ):
        monkeypatch.setattr(nudge, name, forbidden)
    app = typer.Typer()
    app.command()(nudge.nudge_command)
    result = CliRunner().invoke(app, ["--program", program, *mode])
    assert result.exit_code == 2, result.output + repr(result.exception)
    assert "bare program identifier" in result.stderr
    assert not result.stdout
    forbidden.assert_not_called()
    assert not list(tmp_path.iterdir())


def test_nudge_command_preserves_valid_program_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(nudge, "PROGRAMS_ROOT", tmp_path)
    generation = Mock(side_effect=RuntimeError("Selected generation boundary"))
    monkeypatch.setattr(nudge, "generate_full_hygiene_nudges", generation)
    app = typer.Typer()
    app.command()(nudge.nudge_command)
    result = CliRunner().invoke(app, ["--program", "fixture", "--dry-run"])
    assert isinstance(result.exception, RuntimeError)
    assert str(result.exception) == "Selected generation boundary"
    generation.assert_called_once_with(
        program_id="fixture", dry_run=True, stale_overrides=None,
        stale_a=None, stale_b=None, stale_c=None, programs_root=tmp_path,
    )
    assert not list(tmp_path.iterdir())


def test_installed_nudge_rejects_selector_before_mode_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = importlib.import_module("cli")
    for name in (
        "_maybe_register_tier_decision_sink", "_maybe_run_scheduled_db_maintenance",
        "_maybe_run_session_catchup",
    ):
        monkeypatch.setattr(cli, name, Mock())
    monkeypatch.setattr(nudge, "PROGRAMS_ROOT", tmp_path)
    audit = Mock(side_effect=AssertionError("No audit for an invalid selector"))
    monkeypatch.setattr(nudge, "_run_registry_audit", audit)
    result = CliRunner().invoke(
        cli.app, ["nudge", "--program", "../escape", "--audit-registry"],
    )
    assert result.exit_code == 2, result.output + repr(result.exception)
    assert "bare program identifier" in result.stderr
    assert not result.stdout
    audit.assert_not_called()
    assert not list(tmp_path.iterdir())
