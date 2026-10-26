from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import Mock

import click
from click.testing import CliRunner
import dotenv
import pytest
from typer.main import get_command


@pytest.mark.parametrize("command", ["import-reply", "solicit"])
@pytest.mark.parametrize("arguments,exit_code", [
    ([], 0), (["--dry-run"], 0), (["--help"], 0), (["--invalid-option"], 2),
])
def test_offline_context_commands_have_no_unrelated_startup_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str, arguments: list[str], exit_code: int,
) -> None:
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *_args, **_kwargs: False)
    cli = importlib.import_module("cli")
    root = get_command(cli.app)
    assert isinstance(root, click.Group)
    context = root.commands["context"]
    assert isinstance(context, click.Group)
    intake = Mock()
    context.commands[command] = click.Command(
        command, callback=intake, params=[click.Option(["--dry-run"], is_flag=True)],
    )
    hooks = [Mock(side_effect=AssertionError("Offline context commands must not run unrelated startup writes")) for _ in range(3)]
    for name, hook in zip(
        ("_maybe_register_tier_decision_sink", "_maybe_run_scheduled_db_maintenance", "_maybe_run_session_catchup"),
        hooks, strict=True,
    ):
        monkeypatch.setattr(cli, name, hook)
    result = CliRunner().invoke(root, ["context", command, *arguments])
    assert result.exit_code == exit_code, result.output + repr(result.exception)
    for hook in hooks:
        hook.assert_not_called()
    if arguments in (["--help"], ["--invalid-option"]):
        intake.assert_not_called()
    else:
        intake.assert_called_once_with(dry_run=bool(arguments))
    assert not list(tmp_path.iterdir())


def test_other_context_commands_preserve_existing_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *_args, **_kwargs: False)
    cli = importlib.import_module("cli")
    root = get_command(cli.app)
    assert isinstance(root, click.Group)
    context = root.commands["context"]
    assert isinstance(context, click.Group)
    handler = Mock(side_effect=RuntimeError("Synthetic context boundary"))
    context.commands["fixture-operation"] = click.Command("fixture-operation", callback=handler)
    hooks = [Mock() for _ in range(3)]
    for name, hook in zip(
        ("_maybe_register_tier_decision_sink", "_maybe_run_scheduled_db_maintenance", "_maybe_run_session_catchup"),
        hooks, strict=True,
    ):
        monkeypatch.setattr(cli, name, hook)
    result = CliRunner().invoke(root, ["context", "fixture-operation"])
    assert isinstance(result.exception, RuntimeError)
    assert str(result.exception) == "Synthetic context boundary"
    handler.assert_called_once_with()
    for hook in hooks:
        hook.assert_called_once()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("arguments,exit_code", [
    (["--help"], 0), (["--invalid-option"], 2), ([], 2),
])
def test_actual_reply_command_validates_before_unrelated_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arguments: list[str], exit_code: int,
) -> None:
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *_args, **_kwargs: False)
    cli = importlib.import_module("cli")
    hooks = [Mock(side_effect=AssertionError("No startup effects for reply command validation")) for _ in range(3)]
    for name, hook in zip(
        ("_maybe_register_tier_decision_sink", "_maybe_run_scheduled_db_maintenance", "_maybe_run_session_catchup"),
        hooks, strict=True,
    ):
        monkeypatch.setattr(cli, name, hook)
    result = CliRunner().invoke(get_command(cli.app), ["context", "import-reply", *arguments])
    assert result.exit_code == exit_code, result.output + repr(result.exception)
    if arguments == ["--help"]:
        assert "--dry-run" in result.output
    for hook in hooks:
        hook.assert_not_called()
    assert not list(tmp_path.iterdir())
