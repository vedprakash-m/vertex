from __future__ import annotations

import importlib
import json
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from unittest.mock import Mock

import click
from click.testing import CliRunner
import dotenv
import pytest
from typer.main import get_command

from src.core.metric_models import MetricObservation, MetricQualityState
from src.core.reality_store import RealityStore


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


def _metric_history_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[CliRunner, click.Group]:
    monkeypatch.setattr(dotenv, "load_dotenv", Mock(return_value=False))
    cli = importlib.import_module("cli")
    for name in (
        "_maybe_register_tier_decision_sink",
        "_maybe_run_scheduled_db_maintenance",
        "_maybe_run_session_catchup",
    ):
        monkeypatch.setattr(cli, name, Mock())
    root = get_command(cli.app)
    assert isinstance(root, click.Group)
    return CliRunner(), root


def _metric_history_arguments(db_root: Path) -> list[str]:
    return [
        "admin",
        "metric",
        "history",
        "--program",
        "acme",
        "--metric",
        "acme.test_metric",
        "--format",
        "json",
        "--db-root",
        str(db_root),
    ]


def _write_metric_observation(store: RealityStore, observation_id: str, hour: int, value: float) -> None:
    store.write_metric_observation(
        MetricObservation(
            observation_id=observation_id,
            program_id="acme",
            metric_id="acme.test_metric",
            dimensions_json="{}",
            measurement_period_start=datetime(2026, 1, 1, hour, tzinfo=timezone.utc),
            measurement_period_end=datetime(2026, 1, 1, hour + 1, tzinfo=timezone.utc),
            observed_at=datetime(2026, 1, 1, hour + 1, 5, tzinfo=timezone.utc),
            value_num=value,
            value_text=None,
            sample_count=1,
            quality_state=MetricQualityState.OK,
        )
    )


def test_metric_history_reads_ordered_json_without_changing_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_root = tmp_path / "db"
    store = RealityStore("acme", db_root=db_root)
    store.initialize()
    _write_metric_observation(store, "obs-later", 2, 20.0)
    _write_metric_observation(store, "obs-earlier", 1, 10.0)
    before = store.db_path.read_bytes()

    runner, root = _metric_history_invocation(monkeypatch)
    result = runner.invoke(root, _metric_history_arguments(db_root))

    assert result.exit_code == 0, result.output + repr(result.exception)
    payload = json.loads(result.output)
    assert payload["program_id"] == "acme"
    assert payload["metric_id"] == "acme.test_metric"
    assert payload["observation_count"] == 2
    assert [row["value_num"] for row in payload["observations"]] == [10.0, 20.0]
    assert store.db_path.read_bytes() == before


def test_metric_history_returns_valid_empty_history_without_changing_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_root = tmp_path / "db"
    store = RealityStore("acme", db_root=db_root)
    store.initialize()
    before = store.db_path.read_bytes()

    runner, root = _metric_history_invocation(monkeypatch)
    result = runner.invoke(root, _metric_history_arguments(db_root))

    assert result.exit_code == 0, result.output + repr(result.exception)
    assert json.loads(result.output)["observations"] == []
    assert store.db_path.read_bytes() == before


@pytest.mark.parametrize("state", ["missing", "empty", "uninitialized", "incompatible", "corrupt"])
def test_metric_history_fails_safely_without_creating_or_modifying_unavailable_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    db_root = tmp_path / "db"
    store = RealityStore("acme", db_root=db_root)
    path = store.db_path
    if state == "empty":
        path.parent.mkdir(parents=True)
        path.touch()
    elif state == "uninitialized":
        path.parent.mkdir(parents=True)
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE unrelated (value TEXT)")
    elif state == "incompatible":
        path.parent.mkdir(parents=True)
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE reality_metric_observations (program_id TEXT)")
    elif state == "corrupt":
        path.parent.mkdir(parents=True)
        path.write_bytes(b"not a SQLite database")
    before = path.read_bytes() if path.exists() else None

    runner, root = _metric_history_invocation(monkeypatch)
    result = runner.invoke(root, _metric_history_arguments(db_root))

    assert result.exit_code == 2, result.output + repr(result.exception)
    assert "Metric history is unavailable" in result.output
    assert str(path) not in result.output
    assert (path.read_bytes() if path.exists() else None) == before
    if state == "missing":
        assert not path.parent.exists()


def test_metric_history_reports_corrupt_observations_without_changing_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_root = tmp_path / "db"
    store = RealityStore("acme", db_root=db_root)
    store.initialize()
    _write_metric_observation(store, "obs-corrupt", 1, 10.0)
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            "UPDATE reality_metric_observations SET quality_state = ? WHERE observation_id = ?",
            ("not-a-quality-state", "obs-corrupt"),
        )
    before = store.db_path.read_bytes()

    runner, root = _metric_history_invocation(monkeypatch)
    result = runner.invoke(root, _metric_history_arguments(db_root))

    assert result.exit_code == 2, result.output + repr(result.exception)
    assert "Metric history is unavailable" in result.output
    assert str(store.db_path) not in result.output
    assert store.db_path.read_bytes() == before


def test_metric_history_route_skips_startup_writes_but_metric_list_keeps_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dotenv, "load_dotenv", Mock(return_value=False))
    cli = importlib.import_module("cli")
    root = get_command(cli.app)
    assert isinstance(root, click.Group)
    db_root = tmp_path / "db"
    store = RealityStore("acme", db_root=db_root)
    store.initialize()
    hooks = [Mock() for _ in range(3)]
    for name, hook in zip(
        ("_maybe_register_tier_decision_sink", "_maybe_run_scheduled_db_maintenance", "_maybe_run_session_catchup"),
        hooks,
        strict=True,
    ):
        monkeypatch.setattr(cli, name, hook)
    history = CliRunner().invoke(root, _metric_history_arguments(db_root))
    assert history.exit_code == 0, history.output + repr(history.exception)
    for hook in hooks:
        hook.assert_not_called()

    metrics_root = tmp_path / "metrics"
    metrics_root.mkdir()
    monkeypatch.setattr("src.commands.metric.METRICS_ROOT", metrics_root)
    sibling = CliRunner().invoke(root, ["admin", "metric", "list", "--format", "json"])
    assert sibling.exit_code == 0, sibling.output + repr(sibling.exception)
    for hook in hooks:
        hook.assert_called_once()


def test_default_metric_observation_listing_still_initializes_missing_database(tmp_path: Path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "db")

    assert store.list_metric_observations("acme.test_metric") == ()
    assert store.db_path.is_file()
