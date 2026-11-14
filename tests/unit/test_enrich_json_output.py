from __future__ import annotations

import importlib
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import click
import dotenv
import pytest
import typer
from click.testing import CliRunner
from typer.main import get_command

from src.core.evidence_models import SourceRef, WorkstreamEvidence
from src.core.models import RiskLevel


@pytest.fixture
def command_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *_args, **_kwargs: False)
    cli = importlib.import_module("cli")
    enrich = importlib.import_module("src.commands.enrich")
    root = get_command(cli.app)
    assert isinstance(root, click.Group)

    entry = {
        "id": "lane-a",
        "name": "Synthetic lane",
        "signal_sources": {
            "teams_meeting_series": [{"display_name": "Synthetic meeting"}],
        },
    }
    lanes = [entry]
    bridge = Mock()
    bridge.probe.return_value = SimpleNamespace(available=True, has_workiq=True)
    bridge.ask_workiq.return_value = "synthetic response"
    resolved = SimpleNamespace(
        paths=SimpleNamespace(program_id="synthetic"),
        program=SimpleNamespace(ai=None, m365=None),
    )
    evidence = WorkstreamEvidence(
        lane_id="lane-a",
        synthesized_at=datetime(2026, 6, 21, tzinfo=timezone.utc),
        risk_level=RiskLevel.LOW,
        etas=(),
        blocking_items=(),
        owners=(),
        source_refs=(SourceRef(
            "workiq_email", "Synthetic source", None, None,
            canonical_id="native-thread-1",
        ),),
        raw_excerpts=(),
        confidence=0.8,
        narrative_summary="Synthetic evidence",
    )
    extractor = Mock(return_value=evidence)
    catchup = Mock(side_effect=lambda _program, *, emit, **_kwargs: emit("Synthetic catchup"))
    monkeypatch.setattr(cli, "_maybe_register_tier_decision_sink", Mock())
    monkeypatch.setattr(cli, "_maybe_run_scheduled_db_maintenance", Mock())
    monkeypatch.setattr(cli, "_program_for_startup", lambda _ctx: "synthetic")
    monkeypatch.setattr(cli, "_stdout_supports_interactive_catchup", lambda: True)
    monkeypatch.setattr(cli, "build_catchup_event_builder", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli, "maybe_catchup", catchup)
    monkeypatch.setattr(enrich, "_PROGRAMS_ROOT", tmp_path)
    monkeypatch.setattr(enrich, "_EDITIONS_ROOT", tmp_path)
    monkeypatch.setattr(enrich, "resolve_edition", lambda *_args, **_kwargs: resolved)
    monkeypatch.setattr(enrich, "load_program_context", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(enrich, "_load_registry_lanes", lambda *_args: lanes)
    monkeypatch.setattr(enrich, "_build_workiq_cost_guard", lambda **_kwargs: None)
    monkeypatch.setattr(enrich, "_build_workiq_breaker", lambda **_kwargs: None)
    monkeypatch.setattr(enrich, "_is_workiq_lane_suspended", lambda **_kwargs: False)
    monkeypatch.setattr(enrich, "_record_workiq_lane_result", lambda **_kwargs: None)
    monkeypatch.setattr("src.m365.agency_bridge.AgencyBridge", lambda **_kwargs: bridge)
    monkeypatch.setattr("src.ai.content_extractor.ContentExtractionAgent.extract", extractor)
    monkeypatch.setattr(
        "src.commands.gather_pipeline.evidence_extraction_stage.persist_evidence",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        "src.commands.gather_pipeline.evidence_extraction_stage._persist_evidence",
        lambda evidence, **_kwargs: (False, evidence),
    )
    return SimpleNamespace(
        root=root, cli=cli, enrich=enrich, lanes=lanes, bridge=bridge,
        resolved=resolved, evidence=evidence, extractor=extractor, catchup=catchup,
        monkeypatch=monkeypatch,
    )


def _invoke(context: SimpleNamespace, *arguments: str) -> click.testing.Result:
    return CliRunner().invoke(
        context.root, ["enrich", "--edition", "synthetic", *arguments],
    )


@pytest.mark.parametrize("per_thread", [False, True])
@pytest.mark.parametrize("dry_run", [False, True])
def test_json_result_array_separates_progress_and_catchup(
    command_context: SimpleNamespace, per_thread: bool, dry_run: bool,
) -> None:
    if per_thread:
        command_context.resolved.program.m365 = SimpleNamespace(
            retrieval=SimpleNamespace(per_thread_extraction=True),
        )
        command_context.monkeypatch.setattr(
            command_context.enrich, "_extract_per_thread_evidence",
            lambda **_kwargs: ((command_context.evidence,), False),
        )
    arguments = ["--format", "json", "--no-batch"]
    if dry_run:
        arguments.append("--dry-run")

    result = _invoke(command_context, *arguments)

    assert result.exit_code == 0, result.output + repr(result.exception)
    records = json.loads(result.stdout)
    assert records == [{
        "lane_id": "lane-a",
        **({"source_id": "native-thread-1"} if per_thread else {}),
        "risk_level": "low",
        "etas_found": 0,
        "blocking_found": 0,
        "confidence": 0.8,
    }]
    assert "Extracting: lane-a" in result.stderr
    assert "Extracting:" not in result.stdout
    if dry_run:
        command_context.catchup.assert_not_called()
        assert "dry-run:" in result.stderr
    else:
        command_context.catchup.assert_called_once()
        assert "Synthetic catchup" in result.stderr


@pytest.mark.parametrize(
    ("outcome", "message"),
    [
        ("suspended", "skipped"),
        ("cluster_failure", "cluster query failed"),
        ("no_query", "no email/transcript query configured"),
        ("budget", "budget exceeded"),
        ("query_error", "ERROR: WorkIQ query failed"),
        ("no_response", "ERROR: no WorkIQ response"),
        ("extraction_error", "extraction ERROR: WorkIQ extraction failed"),
        ("no_evidence", "no structured evidence found"),
        ("thread_empty", "sources=0"),
    ],
)
def test_json_empty_outcomes_keep_lane_messages_off_stdout(
    command_context: SimpleNamespace, outcome: str, message: str,
) -> None:
    enrich = command_context.enrich
    monkeypatch = command_context.monkeypatch
    arguments = ["--format=json", "--no-batch"]
    if outcome == "suspended":
        monkeypatch.setattr(enrich, "_is_workiq_lane_suspended", lambda **_kwargs: True)
    elif outcome == "cluster_failure":
        arguments.remove("--no-batch")
        key = enrich._lane_cluster_key(command_context.lanes[0])
        monkeypatch.setattr(enrich, "_build_cluster_response_cache", lambda **_kwargs: {key: None})
    elif outcome == "no_query":
        command_context.lanes[0]["signal_sources"] = {}
    elif outcome == "budget":
        from src.ai.client import BudgetExceeded

        guard = Mock()
        guard.check.side_effect = BudgetExceeded("synthetic limit")
        monkeypatch.setattr(enrich, "_build_workiq_cost_guard", lambda **_kwargs: guard)
    elif outcome == "query_error":
        command_context.bridge.ask_workiq.side_effect = RuntimeError("synthetic query failure")
    elif outcome == "no_response":
        command_context.bridge.ask_workiq.return_value = None
    elif outcome == "extraction_error":
        command_context.extractor.side_effect = RuntimeError("synthetic extraction failure")
    elif outcome == "no_evidence":
        command_context.extractor.return_value = None
    else:
        command_context.resolved.program.m365 = SimpleNamespace(
            retrieval=SimpleNamespace(per_thread_extraction=True),
        )
        monkeypatch.setattr(enrich, "_extract_per_thread_evidence", lambda **_kwargs: ((), False))

    result = _invoke(command_context, *arguments)

    assert result.exit_code == 0, result.output + repr(result.exception)
    assert json.loads(result.stdout) == []
    assert message in result.stderr
    assert "synthetic query failure" not in result.stderr
    assert "synthetic extraction failure" not in result.stderr
    assert "Synthetic catchup" in result.stderr


def test_json_partial_results_exclude_progress_and_preserve_result_shape(
    command_context: SimpleNamespace,
) -> None:
    command_context.lanes[0]["signal_sources"] = {}
    command_context.lanes.append({
        "id": "lane-b",
        "name": "Second synthetic lane",
        "signal_sources": {
            "teams_meeting_series": [{"display_name": "Second synthetic meeting"}],
        },
    })

    result = _invoke(command_context, "--format", "json", "--no-batch")

    assert result.exit_code == 0, result.output + repr(result.exception)
    assert json.loads(result.stdout) == [{
        "lane_id": "lane-b", "risk_level": "low", "etas_found": 0,
        "blocking_found": 0, "confidence": 0.8,
    }]
    assert "no email/transcript query configured" in result.stderr
    assert "risk=low" in result.stderr


@pytest.mark.parametrize(
    "arguments",
    [
        ["--format", "json"],
        ["--format=json"],
        ["--format", "human", "--format=json"],
    ],
)
def test_parsed_json_format_routes_interactive_catchup(
    command_context: SimpleNamespace, arguments: list[str],
) -> None:
    command_context.lanes.clear()
    result = _invoke(command_context, *arguments)
    assert result.exit_code == 0, result.output + repr(result.exception)
    assert json.loads(result.stdout) == []
    assert "Synthetic catchup" in result.stderr


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["--format", "human"],
        ["--format=json", "--format", "human"],
        ["--since", "--format=json"],
    ],
)
def test_human_output_and_non_format_values_preserve_stdout(
    command_context: SimpleNamespace, arguments: list[str],
) -> None:
    result = _invoke(command_context, *arguments)
    assert result.exit_code == 0, result.output + repr(result.exception)
    assert "Synthetic catchup" in result.stdout
    assert "Extracting: lane-a" in result.stdout
    assert "Done. 1 lanes extracted." in result.stdout
    assert "Synthetic catchup" not in result.stderr


def test_non_enrich_interactive_catchup_keeps_stdout(command_context: SimpleNamespace) -> None:
    command_context.root.commands["synthetic-other"] = click.Command(
        "synthetic-other",
        params=[click.Option(["--format"])],
        callback=lambda format: typer.echo(f"Other format: {format}"),
    )
    result = CliRunner().invoke(
        command_context.root, ["synthetic-other", "--format", "json"],
    )
    assert result.exit_code == 0, result.output + repr(result.exception)
    assert "Synthetic catchup" in result.stdout
    assert "Other format: json" in result.stdout
    assert "Synthetic catchup" not in result.stderr


def test_json_routes_real_catchup_emitter(command_context: SimpleNamespace) -> None:
    from src.core import catchup_runner

    monkeypatch = command_context.monkeypatch
    result_value = object()
    run_catchup = Mock(return_value=result_value)
    monkeypatch.setattr(command_context.cli, "maybe_catchup", catchup_runner.maybe_catchup)
    monkeypatch.setattr(catchup_runner, "should_run_catchup", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(catchup_runner, "_require_scan_func", lambda _scan_func: Mock())
    monkeypatch.setattr(catchup_runner, "run_catchup", run_catchup)
    monkeypatch.setattr(catchup_runner, "render_catchup_banner", lambda _result: "Real catchup banner")
    command_context.lanes.clear()

    result = _invoke(command_context, "--format", "json")

    assert result.exit_code == 0, result.output + repr(result.exception)
    assert json.loads(result.stdout) == []
    assert "Real catchup banner" in result.stderr
    run_catchup.assert_called_once()


def test_json_early_failure_keeps_stdout_empty(command_context: SimpleNamespace) -> None:
    result = _invoke(command_context, "--format", "json", "--lane", "missing")
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "Synthetic catchup" in result.stderr
    assert "not found" in result.stderr


def test_json_invalid_option_keeps_stdout_empty(command_context: SimpleNamespace) -> None:
    result = _invoke(command_context, "--format", "json", "--invalid-option")
    assert result.exit_code == 2
    assert result.stdout == ""
    assert "Synthetic catchup" in result.stderr
    assert "No such option" in result.stderr
    command_context.bridge.probe.assert_not_called()


def test_json_help_remains_help(command_context: SimpleNamespace) -> None:
    result = _invoke(command_context, "--format", "json", "--help")
    assert result.exit_code == 0, result.output + repr(result.exception)
    assert "Usage:" in result.stdout
    assert "Synthetic catchup" in result.stderr
    command_context.bridge.probe.assert_not_called()


def test_format_after_terminator_does_not_redirect_catchup(
    command_context: SimpleNamespace,
) -> None:
    result = _invoke(command_context, "--", "--format=json")
    assert result.exit_code == 2
    assert "Synthetic catchup" in result.stdout
    assert "Synthetic catchup" not in result.stderr
    command_context.bridge.probe.assert_not_called()
