from __future__ import annotations

import logging
import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from cli import app
from src.commands import gather
from src.commands.doctor_checks.observability_checks import build_diagnose_report
from src.commands.gather_pipeline import channel_runtime
from src.core.channel_registry_store import ChannelRegistryStore
from src.core.exceptions import QueryError
from src.core.failure_taxonomy import _PATTERN_TABLE, classify_exception
from src.core.gather_run_manifest import resolve_latest_committed_manifest
from src.core.gather_state_store import load_gather_state
from src.core.integration_types import IntegrationError as ProviderIntegrationError, RunContext
from src.core.journal import read_signals
from src.core.models_v2 import IntegrationError
from src.core.program_paths import get_channel_registry_path
from src.core.reality_store import RealityStore
from tests.unit.test_ado_provider_error_privacy import _enable_budget, ado, scenario
from tests.unit.test_icm_uil_provider_privacy import _NOW, _PRIVATE, _PROGRAM


_PHASES = (
    ("discovery_saved", "discovery"),
    ("discovery_query", "discovery"),
    ("batch", "hydration"),
    ("detail", "hydration"),
    ("relations", "hydration"),
)


def _fail_at_runtime(
    ado: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, phase: str, error: Exception,
) -> None:
    def fail(*_args: Any, **_kwargs: Any) -> None:
        raise error

    provider = (
        ado.binding.discovery_provider if phase == "discovery"
        else ado.binding.hydration_provider
    )
    monkeypatch.setattr(type(provider), "discover" if phase == "discovery" else "hydrate", fail)


def _run_runtime(
    ado: SimpleNamespace, store: ChannelRegistryStore, *, dry_run: bool = False,
) -> tuple[Any, list[IntegrationError], list[Any]]:
    errors: list[IntegrationError] = []
    outcomes: list[Any] = []
    hydrated, _ = channel_runtime.run_channel(
        ado.binding, store, program_id=_PROGRAM, since=_NOW, verified_at=_NOW,
        run_ctx=RunContext(force_discovery=True, dry_run=dry_run),
        integration_error_sink=errors, channel_outcome_sink=outcomes,
        programs_root=ado.scenario.root,
    )
    return hydrated, errors, outcomes


@pytest.mark.parametrize(("provider_phase", "runtime_phase"), _PHASES)
@pytest.mark.parametrize("gather_v2", [False, True])
@pytest.mark.parametrize("budgeted", [False, True])
def test_actual_gather_contains_escaping_ado_provider_error(
    ado: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    provider_phase: str, runtime_phase: str, gather_v2: bool, budgeted: bool,
) -> None:
    monkeypatch.setattr(gather, "_gather_v2_enabled", lambda: gather_v2)
    if budgeted:
        _enable_budget(monkeypatch)
    ado.client.phase = provider_phase
    ado.client.error = ValueError(f"429 {_PROGRAM} {_PRIVATE}")
    with caplog.at_level(logging.WARNING):
        artifacts = gather.gather_program(
            _PROGRAM, programs_root=ado.scenario.root, as_of=_NOW, force_discovery=True
        )
    state = load_gather_state(_PROGRAM, programs_root=ado.scenario.root)
    manifest = resolve_latest_committed_manifest(_PROGRAM, programs_root=ado.scenario.root)
    report = build_diagnose_report(_PROGRAM, programs_root=ado.scenario.root)
    registry = ChannelRegistryStore(
        get_channel_registry_path(_PROGRAM, programs_root=ado.scenario.root), _PROGRAM
    )
    with sqlite3.connect(RealityStore(_PROGRAM, db_root=ado.scenario.db_root).db_path) as connection:
        ingestion = connection.execute(
            "SELECT source_ref, status, error_message FROM reality_ingestion_runs"
        ).fetchall()
    assert state is not None and manifest is not None and ingestion
    assert gather._semantic_gather_exit_code(artifacts) == 3
    error = next(
        item for item in artifacts.integration_errors
        if item.source == "ado" and item.stage == runtime_phase
    )
    outcome = next(item for item in manifest.channel_outcomes if item.channel == "ado")
    assert error.retryable and error.operator_action
    assert outcome.degraded and outcome.degrade_reason == error.message
    assert ado.client.calls == {
        "discovery_saved": ["saved:bad"],
        "discovery_query": ["saved:bad", "wiql:bad"],
        "batch": ["saved:bad", "wiql:bad", "saved:good", "wiql:good", "batch"],
        "detail": ["saved:bad", "wiql:bad", "saved:good", "wiql:good", "batch", "revisions:101"],
        "relations": [
            "saved:bad", "wiql:bad", "saved:good", "wiql:good",
            "batch", "revisions:101", "comments:101", "relations",
        ],
    }[provider_phase]
    surfaces = {
        "result": repr(artifacts.integration_errors),
        "state": repr(state.integration_error_details),
        "manifest": repr(outcome),
        "registry": repr(registry.recent_deltas("ado")),
        "signals": repr(read_signals(_PROGRAM, programs_root=ado.scenario.root)),
        "ingestion": repr(ingestion),
        "observability": repr(report),
        "logs": caplog.text,
    }
    assert [name for name, value in surfaces.items() if _PRIVATE in value] == []
    assert _PROGRAM not in error.message
    expected = classify_exception(str(ado.client.error))
    assert classify_exception(error.message) == expected
    assert (report.last_failure_category, report.last_failure_retryable, report.last_failure_next_command) == (
        expected.category, expected.retryable, expected.next_command
    )


@pytest.mark.parametrize(("provider_phase", "runtime_phase"), [
    ("discovery_saved", "discovery"), ("batch", "hydration"),
])
def test_actual_cli_contains_escaping_ado_provider_error(
    ado: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, provider_phase: str, runtime_phase: str,
) -> None:
    ado.client.phase = provider_phase
    ado.client.error = RuntimeError(f"429 {_PROGRAM} {_PRIVATE}")
    monkeypatch.setattr(gather, "PROGRAMS_ROOT", ado.scenario.root)
    monkeypatch.setattr(gather, "_surface_alert_banner", lambda *_args, **_kwargs: None)
    result = CliRunner().invoke(app, ["gather", "--program", _PROGRAM, "--force-discovery"])
    assert result.exit_code == 3, result.output
    assert f"ado/{runtime_phase}" in result.output and "Next:" in result.output
    assert _PRIVATE not in result.output


@pytest.mark.parametrize("phase", ["discovery", "hydration"])
@pytest.mark.parametrize("error_type", [QueryError, RuntimeError, ValueError])
@pytest.mark.parametrize("budgeted", [False, True])
def test_runtime_preserves_caught_error_behavior(
    ado: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    phase: str, error_type: type[Exception], budgeted: bool,
) -> None:
    if budgeted:
        _enable_budget(monkeypatch)
    error = error_type(f"429 {_PRIVATE}")
    _fail_at_runtime(ado, monkeypatch, phase, error)
    hydrated, errors, outcomes = _run_runtime(
        ado, ChannelRegistryStore(tmp_path / "registry.sqlite3", _PROGRAM)
    )
    assert (hydrated is None) is (phase == "hydration")
    assert len(errors) == len(outcomes) == 1
    assert (errors[0].source, errors[0].stage, errors[0].retryable) == ("ado", phase, True)
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
        message = channel_runtime._runtime_error_message("ado", phase, error)
        assert _PRIVATE not in message
        assert classify_exception(message) == classify_exception(str(error))


@pytest.mark.parametrize("phase", ["discovery", "hydration"])
def test_dry_run_runtime_failure_remains_degraded(
    ado: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, phase: str,
) -> None:
    _fail_at_runtime(ado, monkeypatch, phase, QueryError(f"429 {_PRIVATE}"))
    hydrated, errors, outcomes = _run_runtime(
        ado, ChannelRegistryStore(tmp_path / "registry.sqlite3", _PROGRAM), dry_run=True
    )
    assert (hydrated is None) is (phase == "hydration")
    assert len(errors) == len(outcomes) == 1 and outcomes[0].degraded
    assert _PRIVATE not in errors[0].message


@pytest.mark.parametrize("phase", ["discovery", "hydration"])
def test_registry_failure_uses_the_same_private_runtime_boundary(
    ado: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, phase: str,
) -> None:
    store = ChannelRegistryStore(tmp_path / "registry.sqlite3", _PROGRAM)

    def fail(*_args: Any, **_kwargs: Any) -> None:
        raise QueryError(f"401 {_PRIVATE}")

    monkeypatch.setattr(store, "active_registrations" if phase == "discovery" else "get_workstream_map", fail)
    hydrated, errors, outcomes = _run_runtime(ado, store)
    assert (hydrated is None) is (phase == "hydration")
    assert len(errors) == len(outcomes) == 1 and outcomes[0].degraded
    assert errors[0].stage == phase and _PRIVATE not in errors[0].message
    assert classify_exception(errors[0].message) == classify_exception("401")


@pytest.mark.parametrize("gather_v2", [False, True])
@pytest.mark.parametrize("budgeted", [False, True])
def test_discovery_failure_still_hydrates_prior_registrations(
    ado: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, gather_v2: bool, budgeted: bool,
) -> None:
    monkeypatch.setattr(gather, "_gather_v2_enabled", lambda: gather_v2)
    first = gather.gather_program(
        _PROGRAM, programs_root=ado.scenario.root, as_of=_NOW, force_discovery=True
    )
    assert gather._semantic_gather_exit_code(first) == 0
    assert first.hydrated_work_item_ids == ("101",)
    ado.client.calls.clear()
    if budgeted:
        _enable_budget(monkeypatch)
    _fail_at_runtime(ado, monkeypatch, "discovery", QueryError(f"429 {_PRIVATE}"))
    second = gather.gather_program(
        _PROGRAM, programs_root=ado.scenario.root, as_of=_NOW, force_discovery=True
    )
    assert gather._semantic_gather_exit_code(second) == 3
    assert second.hydrated_work_item_ids == ("101",)
    assert ado.client.calls == ["batch", "revisions:101", "comments:101", "relations"]
    assert _PRIVATE not in repr(second.integration_errors)


def test_runtime_error_remains_distinct_from_prior_provider_error(
    ado: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    provider = ado.binding.discovery_provider
    original = provider.discover
    provider_error = ProviderIntegrationError(
        source="ado", stage="discovery", retryable=True,
        message="ADO discovery failed; matched pattern: '429'",
    )

    def discover(*args: Any, **kwargs: Any) -> Any:
        return replace(original(*args, **kwargs), errors=(provider_error,))

    def fail_apply(*_args: Any, **_kwargs: Any) -> None:
        raise QueryError(f"429 {_PRIVATE}")

    monkeypatch.setattr(type(provider), "discover", lambda _self, *args, **kwargs: discover(*args, **kwargs))
    store = ChannelRegistryStore(tmp_path / "registry.sqlite3", _PROGRAM)
    monkeypatch.setattr(store, "apply_discovery_result", fail_apply)
    hydrated, errors, outcomes = _run_runtime(ado, store)
    assert hydrated is not None and len(errors) == 2 and len(outcomes) == 1
    assert errors[0].message == provider_error.message
    assert errors[1].message != provider_error.message
    assert all(error.source == "ado" and error.stage == "discovery" for error in errors)
    assert all(_PRIVATE not in error.message for error in errors)
    assert outcomes[0].degraded and outcomes[0].degrade_reason == errors[1].message


@pytest.mark.parametrize("phase", ["discovery", "hydration"])
@pytest.mark.parametrize("budgeted", [False, True])
def test_unexpected_error_still_propagates(
    ado: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, phase: str, budgeted: bool,
) -> None:
    if budgeted:
        _enable_budget(monkeypatch)
    _fail_at_runtime(ado, monkeypatch, phase, TypeError("synthetic unexpected defect"))
    with pytest.raises(TypeError, match="synthetic unexpected defect"):
        _run_runtime(ado, ChannelRegistryStore(tmp_path / "registry.sqlite3", _PROGRAM))


def test_successful_gather_control(ado: SimpleNamespace) -> None:
    artifacts = gather.gather_program(
        _PROGRAM, programs_root=ado.scenario.root, as_of=_NOW, force_discovery=True
    )
    manifest = resolve_latest_committed_manifest(_PROGRAM, programs_root=ado.scenario.root)
    assert gather._semantic_gather_exit_code(artifacts) == 0
    assert manifest is not None
    assert not next(item for item in manifest.channel_outcomes if item.channel == "ado").degraded
