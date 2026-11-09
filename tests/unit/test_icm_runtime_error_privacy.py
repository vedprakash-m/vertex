from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from cli import app
from src.commands import gather
from src.commands.doctor_checks.observability_checks import build_diagnose_report
from src.commands.gather_pipeline import channel_runtime
from src.core.channel_execution_policy import ChannelExecutionPolicy
from src.core.channel_registry_store import ChannelRegistryStore
from src.core.exceptions import QueryError
from src.core.failure_taxonomy import _PATTERN_TABLE, classify_exception
from src.core.gather_run_manifest import resolve_latest_committed_manifest
from src.core.gather_state_store import load_gather_state
from src.core.integration_types import (
    ADOHydrationOutput,
    ChannelConfig,
    DiscoveryCompleteness,
    DiscoveryResult,
    HydrationResult,
    RunContext,
)
from src.core.reality_store import RealityStore
from tests.unit.test_icm_uil_provider_privacy import (
    _INCIDENT,
    _NOW,
    _PRIVATE,
    _PROGRAM,
    _gather,
    scenario,
)


def _fail_at_runtime(
    scenario: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    error: Exception,
) -> None:
    def fail(*args: Any, **kwargs: Any) -> None:
        scenario.state.calls.append(f"{phase}_runtime")
        raise error

    provider = (
        scenario.binding.discovery_provider
        if phase == "discovery"
        else scenario.binding.hydration_provider
    )
    monkeypatch.setattr(type(provider), "discover" if phase == "discovery" else "hydrate", fail)


def _enable_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    policy = ChannelExecutionPolicy(
        channel="icm",
        required=False,
        inline_allowed=True,
        per_attempt_timeout_seconds=30,
        total_budget_seconds=60,
        max_pages=None,
        max_records=None,
        stale_fallback_allowed=False,
        prefetch_required=False,
    )
    monkeypatch.setattr(channel_runtime, "load_arch_data_fix", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(channel_runtime, "channel_execution_policy_for", lambda *_args, **_kwargs: policy)


def _run_runtime(scenario: SimpleNamespace, path: Path) -> tuple[Any, list[Any], list[Any]]:
    errors: list[Any] = []
    outcomes: list[Any] = []
    hydrated, _ = channel_runtime.run_channel(
        scenario.binding,
        ChannelRegistryStore(path, _PROGRAM),
        program_id=_PROGRAM,
        since=_NOW,
        verified_at=_NOW,
        run_ctx=RunContext(force_discovery=True),
        integration_error_sink=errors,
        channel_outcome_sink=outcomes,
        programs_root=scenario.root,
    )
    return hydrated, errors, outcomes


@pytest.mark.parametrize("phase", ["discovery", "hydration"])
@pytest.mark.parametrize("gather_v2", [False, True])
@pytest.mark.parametrize("budgeted", [False, True])
def test_actual_gather_runtime_diagnostics_omit_provider_text(
    scenario: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, phase: str, gather_v2: bool, budgeted: bool,
) -> None:
    monkeypatch.setattr(gather, "_gather_v2_enabled", lambda: gather_v2)
    if budgeted:
        _enable_budget(monkeypatch)
    _fail_at_runtime(scenario, monkeypatch, phase, QueryError(f"429 {_PROGRAM} {_PRIVATE}"))
    artifacts = _gather(scenario)
    state = load_gather_state(_PROGRAM, programs_root=scenario.root)
    manifest = resolve_latest_committed_manifest(_PROGRAM, programs_root=scenario.root)
    report = build_diagnose_report(_PROGRAM, programs_root=scenario.root)
    assert state is not None and manifest is not None
    assert gather._semantic_gather_exit_code(artifacts) == 2
    error = next(item for item in artifacts.integration_errors if item.stage == phase)
    assert error.source == "icm" and error.retryable and error.operator_action
    outcome = next(item for item in manifest.channel_outcomes if item.channel == "icm")
    assert outcome.degraded and outcome.degrade_reason == error.message
    assert scenario.state.calls == (
        ["discovery_runtime"] if phase == "discovery"
        else ["init", "discovery_query", "hydration_runtime"]
    )
    surfaces = {
        "result": repr(artifacts.integration_errors),
        "state": repr(state.integration_error_details),
        "manifest": repr(manifest.channel_outcomes),
        "observability": repr(report),
    }
    assert [name for name, value in surfaces.items() if _PRIVATE in value] == []
    assert _PROGRAM not in error.message
    expected = classify_exception(f"429 {_PROGRAM} {_PRIVATE}")
    assert (report.last_failure_category, report.last_failure_retryable, report.last_failure_next_command) == (
        expected.category, expected.retryable, expected.next_command
    )


@pytest.mark.parametrize(
    ("phase", "error_type"),
    [("hydration_init", RuntimeError), ("hydration_query", ValueError)],
)
@pytest.mark.parametrize("gather_v2", [False, True])
@pytest.mark.parametrize("budgeted", [False, True])
def test_actual_provider_escaping_failure_is_private(
    scenario: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    phase: str, error_type: type[Exception], gather_v2: bool, budgeted: bool,
) -> None:
    monkeypatch.setattr(gather, "_gather_v2_enabled", lambda: gather_v2)
    if budgeted:
        _enable_budget(monkeypatch)
    scenario.state.phase = phase
    scenario.state.error = error_type(f"429 {_PROGRAM} {_PRIVATE}")
    with caplog.at_level(logging.WARNING):
        artifacts = _gather(scenario)
    state = load_gather_state(_PROGRAM, programs_root=scenario.root)
    manifest = resolve_latest_committed_manifest(_PROGRAM, programs_root=scenario.root)
    report = build_diagnose_report(_PROGRAM, programs_root=scenario.root)
    db_path = RealityStore(_PROGRAM, db_root=scenario.db_root).db_path
    with sqlite3.connect(db_path) as connection:
        ingestion = connection.execute(
            "SELECT status, error_message FROM reality_ingestion_runs WHERE source_ref = ?", ("icm",)
        ).fetchall()
    assert state is not None and manifest is not None
    assert gather._semantic_gather_exit_code(artifacts) == 2
    assert ingestion and all(row[0] == "failed" for row in ingestion)
    assert scenario.state.calls == {
        "hydration_init": ["init", "discovery_query", "init"],
        "hydration_query": ["init", "discovery_query", "init", "hydration_query"],
    }[phase]
    error = next(item for item in artifacts.integration_errors if item.stage == "hydration")
    assert error.source == "icm" and error.retryable and _PROGRAM not in error.message
    outcome = next(item for item in manifest.channel_outcomes if item.channel == "icm")
    assert outcome.degraded and outcome.degrade_reason == error.message
    surfaces = {
        "result": repr(artifacts.integration_errors),
        "state": repr(state.integration_error_details),
        "manifest": repr(manifest.channel_outcomes),
        "ingestion": repr(ingestion),
        "observability": repr(report),
        "logs": caplog.text,
    }
    assert [name for name, value in surfaces.items() if _PRIVATE in value] == []
    expected = classify_exception(str(scenario.state.error))
    assert (report.last_failure_category, report.last_failure_retryable, report.last_failure_next_command) == (
        expected.category, expected.retryable, expected.next_command
    )


@pytest.mark.parametrize("gather_v2", [False, True])
@pytest.mark.parametrize("budgeted", [False, True])
def test_discovery_runtime_failure_hydrates_existing_incidents(
    scenario: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, gather_v2: bool, budgeted: bool,
) -> None:
    monkeypatch.setattr(gather, "_gather_v2_enabled", lambda: gather_v2)
    if budgeted:
        _enable_budget(monkeypatch)
    assert gather._semantic_gather_exit_code(_gather(scenario)) == 0
    assert scenario.state.requested == [_INCIDENT]
    scenario.state.calls.clear()
    scenario.state.requested.clear()
    _fail_at_runtime(scenario, monkeypatch, "discovery", QueryError(f"429 {_PRIVATE}"))
    artifacts = _gather(scenario)
    manifest = resolve_latest_committed_manifest(_PROGRAM, programs_root=scenario.root)
    assert gather._semantic_gather_exit_code(artifacts) == 2
    assert scenario.state.calls == ["discovery_runtime", "init", "hydration_query"]
    assert scenario.state.requested == [_INCIDENT]
    assert manifest is not None
    outcome = next(item for item in manifest.channel_outcomes if item.channel == "icm")
    assert outcome.degraded and outcome.degrade_reason is not None
    assert _PRIVATE not in outcome.degrade_reason


@pytest.mark.parametrize("phase", ["discovery", "hydration"])
def test_actual_cli_runtime_diagnostics_omit_provider_text(
    scenario: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, phase: str,
) -> None:
    _fail_at_runtime(scenario, monkeypatch, phase, QueryError(f"429 {_PROGRAM} {_PRIVATE}"))
    monkeypatch.setattr(gather, "PROGRAMS_ROOT", scenario.root)
    monkeypatch.setattr(gather, "_surface_alert_banner", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(gather, "_load_ado_items_via_uil", lambda *_args, **_kwargs: ((), (), 0))
    monkeypatch.setattr(
        gather, "_resolve_uil_channel_binding_for_gather", lambda *_args, **_kwargs: scenario.binding
    )
    result = CliRunner().invoke(app, ["gather", "--program", _PROGRAM, "--icm", "--force-discovery"])
    assert result.exit_code == 2, result.output
    assert f"icm/{phase}" in result.output and "Next:" in result.output
    assert _PRIVATE not in result.output


@pytest.mark.parametrize("phase", ["discovery", "hydration"])
@pytest.mark.parametrize("error_type", [QueryError, RuntimeError, ValueError])
@pytest.mark.parametrize("budgeted", [False, True])
def test_caught_error_classes_preserve_runtime_behavior(
    scenario: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    phase: str,
    error_type: type[Exception],
    budgeted: bool,
) -> None:
    if budgeted:
        _enable_budget(monkeypatch)
    _fail_at_runtime(scenario, monkeypatch, phase, error_type(f"429 {_PRIVATE}"))
    hydrated, errors, outcomes = _run_runtime(scenario, tmp_path / "registry.sqlite3")
    assert (hydrated is None) is (phase == "hydration")
    assert len(errors) == len(outcomes) == 1
    assert (errors[0].source, errors[0].stage, errors[0].retryable) == ("icm", phase, True)
    assert outcomes[0].degraded and outcomes[0].degrade_reason == errors[0].message
    assert _PRIVATE not in errors[0].message
    assert classify_exception(errors[0].message).category == classify_exception("429").category


@pytest.mark.parametrize("phase", ["discovery", "hydration"])
def test_all_taxonomy_patterns_preserve_classification(
    scenario: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, phase: str,
) -> None:
    texts = [pattern for _, patterns, _ in _PATTERN_TABLE for pattern in patterns]
    texts.extend(("", "novel", "401 and 429"))
    for text in texts:
        original = f"{text} {_PRIVATE}"
        _fail_at_runtime(scenario, monkeypatch, phase, QueryError(original))
        _, errors, _ = _run_runtime(scenario, tmp_path / "registry.sqlite3")
        assert len(errors) == 1 and _PRIVATE not in errors[0].message
        expected = classify_exception(original)
        actual = classify_exception(errors[0].message)
        assert (actual.category, actual.retryable, actual.next_command) == (
            expected.category, expected.retryable, expected.next_command
        )


def test_runtime_error_remains_distinct_from_prior_provider_error(
    scenario: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    scenario.state.phase = "discovery_query"
    scenario.state.error = QueryError(f"429 {_PRIVATE}")
    store = ChannelRegistryStore(tmp_path / "registry.sqlite3", _PROGRAM)

    def fail_apply(*_args: Any, **_kwargs: Any) -> None:
        raise QueryError(f"429 {_PRIVATE}")

    monkeypatch.setattr(store, "apply_discovery_result", fail_apply)
    errors: list[Any] = []
    outcomes: list[Any] = []
    hydrated, _ = channel_runtime.run_channel(
        scenario.binding, store, program_id=_PROGRAM, since=_NOW, verified_at=_NOW,
        run_ctx=RunContext(force_discovery=True), integration_error_sink=errors,
        channel_outcome_sink=outcomes, programs_root=scenario.root,
    )
    assert hydrated is not None and len(errors) == 2
    assert errors[0].message != errors[1].message
    assert all(error.source == "icm" and error.stage == "discovery" for error in errors)
    assert all(_PRIVATE not in error.message for error in errors)
    assert outcomes[0].degraded and outcomes[0].degrade_reason == errors[1].message


@pytest.mark.parametrize("channel", ["teams"])
@pytest.mark.parametrize("phase", ["discovery", "hydration"])
def test_other_channel_runtime_errors_are_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, channel: str, phase: str,
) -> None:
    raw_message = f"429 synthetic {channel} diagnostic"

    def discover(*_args: Any, **_kwargs: Any) -> DiscoveryResult:
        if phase == "discovery":
            raise QueryError(raw_message)
        return DiscoveryResult(
            channel=channel, program_id=_PROGRAM, discovered_refs=(),
            completeness=DiscoveryCompleteness.FULL, scope_statuses={},
            scope_state_updates={}, errors=(), computed_at=_NOW,
        )

    def hydrate(*_args: Any, **_kwargs: Any) -> HydrationResult[ADOHydrationOutput]:
        if phase == "hydration":
            raise QueryError(raw_message)
        return HydrationResult(
            channel=channel, resources=ADOHydrationOutput(work_items=(), freshness_items=()),
            api_call_count=0, hydrated_ref_ids=(), failed_ref_ids=(), errors=(),
        )

    binding = SimpleNamespace(
        config=ChannelConfig(channel=channel, enabled=True, discovery_threshold_hours=24, ttl_days=30),
        discovery_provider=SimpleNamespace(discover=discover),
        hydration_provider=SimpleNamespace(hydrate=hydrate),
        discovery_config=object(), hydration_config=object(),
    )
    monkeypatch.setattr(channel_runtime, "load_arch_data_fix", lambda *_args, **_kwargs: None)
    errors: list[Any] = []
    outcomes: list[Any] = []
    channel_runtime.run_channel(
        binding, ChannelRegistryStore(tmp_path / "registry.sqlite3", _PROGRAM),
        program_id=_PROGRAM, since=_NOW, verified_at=_NOW, run_ctx=RunContext(force_discovery=True),
        integration_error_sink=errors, channel_outcome_sink=outcomes, programs_root=tmp_path,
    )
    assert len(errors) == len(outcomes) == 1
    assert errors[0].message == raw_message
    assert outcomes[0].degraded and outcomes[0].degrade_reason == raw_message


@pytest.mark.parametrize("phase", ["discovery", "hydration"])
@pytest.mark.parametrize("budgeted", [False, True])
def test_unexpected_error_still_propagates(
    scenario: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, phase: str, budgeted: bool,
) -> None:
    if budgeted:
        _enable_budget(monkeypatch)
    _fail_at_runtime(scenario, monkeypatch, phase, TypeError("synthetic unexpected defect"))
    with pytest.raises(TypeError, match="synthetic unexpected defect"):
        _gather(scenario)


def test_successful_actual_gather_control(scenario: SimpleNamespace) -> None:
    artifacts = _gather(scenario)
    assert artifacts.integration_error_count == 0
    assert gather._semantic_gather_exit_code(artifacts) == 0
