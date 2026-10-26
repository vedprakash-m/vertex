from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import Mock

import click
from click.testing import CliRunner
import dotenv
import pytest
from typer.main import get_command


@pytest.fixture
def nudge_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[click.Group, Mock, list[Mock]]:
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *_args, **_kwargs: False)
    cli = importlib.import_module("cli")
    root = get_command(cli.app)
    assert isinstance(root, click.Group)
    handler = Mock()
    root.commands["nudge"] = click.Command(
        "nudge",
        callback=handler,
        params=[
            click.Option(["--program"]),
            click.Option(["--note"]),
            click.Option(["--approve-draft"]),
            click.Option(["--mark-sent"]),
        ],
    )
    hooks = [Mock() for _ in range(3)]
    for name, hook in zip(
        ("_maybe_register_tier_decision_sink", "_maybe_run_scheduled_db_maintenance", "_maybe_run_session_catchup"),
        hooks, strict=True,
    ):
        monkeypatch.setattr(cli, name, hook)
    return root, handler, hooks


@pytest.mark.parametrize("arguments,exit_code,handled", [
    (["--approve-draft", "packet.eml"], 0, True),
    (["--mark-sent", "packet.eml"], 0, True),
    (["--approve-draft=packet.eml"], 0, True),
    (["--mark-sent=packet.eml"], 0, True),
    (["--approve-draft="], 0, True),
    (["--approve-draft", "packet.eml", "--help"], 0, False),
    (["--help", "--mark-sent", "packet.eml"], 0, False),
    (["--approve-draft", "packet.eml", "--invalid-option"], 2, False),
    (["--invalid-option", "--mark-sent", "packet.eml"], 2, False),
    (["--approve-draft"], 2, False),
    (["--mark-sent"], 2, False),
])
def test_nudge_lifecycle_modes_skip_unrelated_startup(
    tmp_path: Path,
    nudge_startup: tuple[click.Group, Mock, list[Mock]],
    arguments: list[str],
    exit_code: int,
    handled: bool,
) -> None:
    root, handler, hooks = nudge_startup
    result = CliRunner().invoke(root, ["nudge", *arguments])
    assert result.exit_code == exit_code, result.output + repr(result.exception)
    assert handler.call_count == int(handled)
    for hook in hooks:
        hook.assert_not_called()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("arguments", [
    [],
    ["--program", "synthetic"],
    ["--program=--approve-draft"],
    ["--note", "--approve-draft"],
    ["--note=--mark-sent"],
])
def test_other_nudge_modes_preserve_existing_startup(
    tmp_path: Path,
    nudge_startup: tuple[click.Group, Mock, list[Mock]],
    arguments: list[str],
) -> None:
    root, handler, hooks = nudge_startup
    result = CliRunner().invoke(root, ["nudge", *arguments])
    assert result.exit_code == 0, result.output + repr(result.exception)
    handler.assert_called_once()
    for hook in hooks:
        hook.assert_called_once()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("preview,missing", [(True, False), (False, False), (False, True)])
def test_nudge_startup_classification_does_not_validate_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    nudge_startup: tuple[click.Group, Mock, list[Mock]],
    preview: bool,
    missing: bool,
) -> None:
    root, handler, hooks = nudge_startup
    path_type = click.Path(exists=True, file_okay=False, path_type=Path)
    option = click.Option(["--programs-root"], type=path_type, default=tmp_path)
    root.commands["nudge"].params.append(option)
    convert = click.Path.convert
    conversions: list[object] = []

    def checked_convert(
        self: click.Path, value: object, param: click.Parameter | None, ctx: click.Context | None,
    ) -> object:
        assert ctx is not None
        assert ctx.help_option_names != [], "Startup classification must not inspect filesystem paths"
        conversions.append(value)
        return convert(self, value, param, ctx)

    monkeypatch.setattr(click.Path, "convert", checked_convert)
    arguments = ["nudge", "--approve-draft", "packet.eml"]
    if preview:
        arguments.append("--help")
    if missing:
        arguments.extend(["--programs-root", str(tmp_path / "missing")])
    result = CliRunner().invoke(root, arguments)
    assert result.exit_code == (2 if missing else 0), result.output + repr(result.exception)
    assert handler.call_count == int(not preview and not missing)
    assert len(conversions) == int(not preview)
    assert option.type is path_type
    for hook in hooks:
        hook.assert_not_called()
    assert not list(tmp_path.iterdir())
