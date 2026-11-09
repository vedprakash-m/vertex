from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from cli import app
from src.commands import gather
from src.commands.doctor_checks.observability_checks import build_diagnose_report
from src.commands.gather_pipeline import channel_runtime
from src.core import kusto_discovery, kusto_hydration
from src.core.channel_execution_policy import ChannelExecutionPolicy
from src.core.channel_registry_store import ChannelRegistryStore
from src.core.exceptions import QueryError
from src.core.failure_taxonomy import _PATTERN_TABLE, classify_exception
from src.core.gather_run_manifest import resolve_latest_committed_manifest
from src.core.gather_state_store import load_gather_state
from src.core.integration_types import ChannelBinding, ChannelConfig, RunContext
from src.core.journal import read_signals
from src.core.kusto_discovery import KustoDiscoveryConfig, KustoDiscoveryProvider
from src.core.kusto_hydration import KustoHydrationConfig, KustoHydrationProvider
from src.core.kusto_signal_extractor import KustoSignalExtractor
from src.core.models_v2 import KustoQuery
from tests.unit.test_kusto_error_privacy import _NOW, _PRIVATE, _PROGRAM, scenario


_REAL_UIL_LOADER = gather._load_kusto_signals_via_uil


@pytest.fixture
def runtime(scenario: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    query = KustoQuery(
        id="synthetic-query", cluster="https://example.test", database="Example",
        kql="Synthetic | take 1", section="Synthetic", render_as="table",
        confidence="high", program_ids=(_PROGRAM,), validated=True,
    )
    calls: list[str] = []

    def execute(query: KustoQuery) -> list[dict[str, Any]]:
        calls.append(query.id)
        return [{"Value": 1}]

    def load_queries(*_args: object, **_kwargs: object) -> tuple[KustoQuery, ...]:
        return (query,)

    binding = ChannelBinding(
        config=ChannelConfig(channel="kusto", enabled=True, discovery_threshold_hours=24, ttl_days=30),
        discovery_provider=KustoDiscoveryProvider(query_loader=load_queries),
        discovery_config=KustoDiscoveryConfig(programs_root=scenario.root),
        hydration_provider=KustoHydrationProvider(executor=execute, query_loader=load_queries),
        hydration_config=KustoHydrationConfig(programs_root=scenario.root),
        signal_extractor=KustoSignalExtractor(),
    )
    monkeypatch.setattr(gather, "_load_kusto_signals_via_uil", _REAL_UIL_LOADER)
    monkeypatch.setattr(
        gather, "_resolve_uil_channel_binding_for_gather",
        lambda _program, _workstreams, channel, **_kwargs: binding if channel == "kusto" else None,
    )
    monkeypatch.setattr(channel_runtime, "load_arch_data_fix", lambda *_args, **_kwargs: None)
    return SimpleNamespace(scenario=scenario, binding=binding, calls=calls, execute=execute)


def _gather(runtime: SimpleNamespace) -> Any:
    return gather.gather_program(
        _PROGRAM, programs_root=runtime.scenario.root, as_of=_NOW,
        loader=lambda *_args, **_kwargs: ((), 0),
        freshness_loader=lambda *_args, **_kwargs: ((), 0),
        include_kusto=True, kusto_query_executor=runtime.execute, force_discovery=True,
    )


def _fail_in_actual_provider(monkeypatch: pytest.MonkeyPatch, phase: str, error: Exception) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise error

    module, function = (
        (kusto_discovery, "_query_to_discovered_ref")
        if phase == "discovery" else (kusto_hydration, "render_kusto_query")
    )
    monkeypatch.setattr(module, function, fail)


def _fail_at_runtime(
    runtime: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, phase: str, error: Exception,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise error

    provider = (
        runtime.binding.discovery_provider if phase == "discovery"
        else runtime.binding.hydration_provider
    )
    monkeypatch.setattr(type(provider), "discover" if phase == "discovery" else "hydrate", fail)


def _enable_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    policy = ChannelExecutionPolicy(
        channel="kusto", required=False, inline_allowed=True,
        per_attempt_timeout_seconds=30, total_budget_seconds=60,
        max_pages=None, max_records=None, stale_fallback_allowed=False,
        prefetch_required=False,
    )
    monkeypatch.setattr(channel_runtime, "load_arch_data_fix", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(channel_runtime, "channel_execution_policy_for", lambda *_args, **_kwargs: policy)


def _run_runtime(
    runtime: SimpleNamespace, store: ChannelRegistryStore, *, dry_run: bool = False,
) -> tuple[Any, list[Any], list[Any]]:
    errors: list[Any] = []
    outcomes: list[Any] = []
    hydrated, _ = channel_runtime.run_channel(
        runtime.binding, store, program_id=_PROGRAM, since=_NOW, verified_at=_NOW,
        run_ctx=RunContext(force_discovery=True, dry_run=dry_run),
        integration_error_sink=errors, channel_outcome_sink=outcomes,
        programs_root=runtime.scenario.root,
    )
    return hydrated, errors, outcomes


@pytest.mark.parametrize("phase", ["discovery", "hydration"])
@pytest.mark.parametrize("gather_v2", [False, True])
@pytest.mark.parametrize("budgeted", [False, True])
@pytest.mark.parametrize("error_type", [QueryError, RuntimeError, ValueError])
def test_actual_gather_runtime_diagnostics_omit_provider_text(
    runtime: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    phase: str, gather_v2: bool, budgeted: bool, error_type: type[Exception],
) -> None:
    monkeypatch.setattr(gather, "_gather_v2_enabled", lambda: gather_v2)
    if budgeted:
        _enable_budget(monkeypatch)
    error = error_type(f"429 {_PROGRAM} {_PRIVATE}")
    _fail_in_actual_provider(monkeypatch, phase, error)
    with caplog.at_level(logging.WARNING):
        artifacts = _gather(runtime)
    state = load_gather_state(_PROGRAM, programs_root=runtime.scenario.root)
    manifest = resolve_latest_committed_manifest(_PROGRAM, programs_root=runtime.scenario.root)
    report = build_diagnose_report(_PROGRAM, programs_root=runtime.scenario.root)
    assert state is not None and manifest is not None
    assert runtime.calls == []
    assert artifacts.integration_error_count == 1
    assert gather._semantic_gather_exit_code(artifacts) == 2
    diagnostic = artifacts.integration_errors[0]
    assert (diagnostic.source, diagnostic.stage, diagnostic.retryable) == ("kusto", phase, True)
    assert diagnostic.operator_action and _PROGRAM not in diagnostic.message
    outcome = next(item for item in manifest.channel_outcomes if item.channel == "kusto")
    assert outcome.degraded and outcome.degrade_reason == diagnostic.message
    assert state.channels["kusto"]["last_error"] == diagnostic.message
    expected = classify_exception(str(error))
    assert classify_exception(diagnostic.message) == expected
    assert (report.last_failure_category, report.last_failure_retryable, report.last_failure_next_command) == (
        expected.category, expected.retryable, expected.next_command,
    )
    surfaces = {
        "result": repr(artifacts.integration_errors),
        "state": repr(state.integration_error_details) + repr(state.channels),
        "manifest": repr(manifest.channel_outcomes) + repr(manifest.failed_refs),
        "signals": repr(tuple(read_signals(_PROGRAM, programs_root=runtime.scenario.root))),
        "observability": repr(report),
        "logs": caplog.text,
    }
    assert [name for name, value in surfaces.items() if _PRIVATE in value] == []


@pytest.mark.parametrize("phase", ["discovery", "hydration"])
def test_actual_cli_runtime_diagnostics_omit_provider_text(
    runtime: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, phase: str,
) -> None:
    _fail_in_actual_provider(monkeypatch, phase, QueryError(f"429 {_PROGRAM} {_PRIVATE}"))
    monkeypatch.setattr(gather, "PROGRAMS_ROOT", runtime.scenario.root)
    monkeypatch.setattr(gather, "_surface_alert_banner", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(gather, "_load_ado_items_via_uil", lambda *_args, **_kwargs: ((), (), 0))
    monkeypatch.setattr(
        gather, "_resolve_uil_channel_binding_for_gather",
        lambda _program, _workstreams, channel, **_kwargs: (
            runtime.binding if channel == "kusto" else object() if channel == "ado" else None
        ),
    )
    result = CliRunner().invoke(app, ["gather", "--program", _PROGRAM, "--kusto", "--force-discovery"])
    assert result.exit_code == 2, result.output
    assert f"kusto/{phase}" in result.output and "Next:" in result.output
    assert _PRIVATE not in result.output


@pytest.mark.parametrize("phase", ["discovery", "hydration"])
@pytest.mark.parametrize("error_type", [QueryError, RuntimeError, ValueError])
@pytest.mark.parametrize("budgeted", [False, True])
@pytest.mark.parametrize("dry_run", [False, True])
def test_caught_runtime_error_preserves_failure_behavior(
    runtime: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    phase: str, error_type: type[Exception], budgeted: bool, dry_run: bool,
) -> None:
    if budgeted:
        _enable_budget(monkeypatch)
    error = error_type(f"429 {_PRIVATE}")
    _fail_at_runtime(runtime, monkeypatch, phase, error)
    hydrated, errors, outcomes = _run_runtime(
        runtime, ChannelRegistryStore(tmp_path / "registry.sqlite3", _PROGRAM), dry_run=dry_run,
    )
    assert (hydrated is None) is (phase == "hydration")
    assert len(errors) == len(outcomes) == 1
    assert (errors[0].source, errors[0].stage, errors[0].retryable) == ("kusto", phase, True)
    assert errors[0].operator_action and outcomes[0].degraded
    assert outcomes[0].degrade_reason == errors[0].message
    assert _PRIVATE not in errors[0].message
    assert classify_exception(errors[0].message) == classify_exception(str(error))


@pytest.mark.parametrize("phase", ["discovery", "hydration"])
def test_every_taxonomy_pattern_is_retained_without_exception_text(phase: str) -> None:
    patterns = [pattern for _, group, _ in _PATTERN_TABLE for pattern in group]
    patterns.extend(("", "novel", "401 and 429"))
    for pattern in patterns:
        error = QueryError(f"{pattern} {_PRIVATE}")
        message = channel_runtime._runtime_error_message("kusto", phase, error)
        assert _PRIVATE not in message
        assert classify_exception(message) == classify_exception(str(error))


@pytest.mark.parametrize("phase", ["discovery", "hydration"])
def test_registry_failure_uses_the_same_private_runtime_boundary(
    runtime: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, phase: str,
) -> None:
    store = ChannelRegistryStore(tmp_path / "registry.sqlite3", _PROGRAM)

    def fail(*_args: object, **_kwargs: object) -> None:
        raise QueryError(f"401 {_PRIVATE}")

    monkeypatch.setattr(store, "active_registrations" if phase == "discovery" else "get_workstream_map", fail)
    hydrated, errors, outcomes = _run_runtime(runtime, store)
    assert (hydrated is None) is (phase == "hydration")
    assert len(errors) == len(outcomes) == 1 and outcomes[0].degraded
    assert errors[0].stage == phase and _PRIVATE not in errors[0].message
    assert classify_exception(errors[0].message) == classify_exception("401")


def test_reused_sink_coalesces_matching_safe_runtime_diagnostics(
    runtime: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    store = ChannelRegistryStore(tmp_path / "registry.sqlite3", _PROGRAM)
    errors: list[Any] = []
    outcomes: list[Any] = []
    for detail in ("first", "second"):
        _fail_at_runtime(runtime, monkeypatch, "discovery", QueryError(f"429 {detail} {_PRIVATE}"))
        channel_runtime.run_channel(
            runtime.binding, store, program_id=_PROGRAM, since=_NOW, verified_at=_NOW,
            run_ctx=RunContext(force_discovery=True), integration_error_sink=errors,
            channel_outcome_sink=outcomes, programs_root=runtime.scenario.root,
        )
    assert len(errors) == 1 and len(outcomes) == 2
    assert all(outcome.degraded and outcome.degrade_reason == errors[0].message for outcome in outcomes)
    assert _PRIVATE not in errors[0].message


@pytest.mark.parametrize("gather_v2", [False, True])
@pytest.mark.parametrize("budgeted", [False, True])
def test_discovery_failure_still_hydrates_prior_registrations(
    runtime: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, gather_v2: bool, budgeted: bool,
) -> None:
    monkeypatch.setattr(gather, "_gather_v2_enabled", lambda: gather_v2)
    assert gather._semantic_gather_exit_code(_gather(runtime)) == 0
    assert runtime.calls == ["synthetic-query"]
    runtime.calls.clear()
    if budgeted:
        _enable_budget(monkeypatch)
    _fail_in_actual_provider(monkeypatch, "discovery", QueryError(f"429 {_PRIVATE}"))
    artifacts = _gather(runtime)
    manifest = resolve_latest_committed_manifest(_PROGRAM, programs_root=runtime.scenario.root)
    assert runtime.calls == ["synthetic-query"]
    assert artifacts.integration_error_count == 1 and gather._semantic_gather_exit_code(artifacts) == 2
    assert _PRIVATE not in repr(artifacts.integration_errors)
    assert manifest is not None
    outcome = next(item for item in manifest.channel_outcomes if item.channel == "kusto")
    assert outcome.degraded and outcome.degrade_reason == artifacts.integration_errors[0].message


@pytest.mark.parametrize("phase", ["discovery", "hydration"])
@pytest.mark.parametrize("budgeted", [False, True])
def test_unexpected_error_still_propagates(
    runtime: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, phase: str, budgeted: bool,
) -> None:
    if budgeted:
        _enable_budget(monkeypatch)
    _fail_in_actual_provider(monkeypatch, phase, TypeError("synthetic unexpected defect"))
    with pytest.raises(TypeError, match="synthetic unexpected defect"):
        _gather(runtime)


def test_successful_actual_gather_control(runtime: SimpleNamespace) -> None:
    artifacts = _gather(runtime)
    manifest = resolve_latest_committed_manifest(_PROGRAM, programs_root=runtime.scenario.root)
    assert runtime.calls == ["synthetic-query"]
    assert artifacts.integration_error_count == 0 and gather._semantic_gather_exit_code(artifacts) == 0
    assert manifest is not None
    assert not next(item for item in manifest.channel_outcomes if item.channel == "kusto").degraded
