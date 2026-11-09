from __future__ import annotations

import logging
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from cli import app
from src.commands import gather
from src.commands.doctor_checks.observability_checks import build_diagnose_report
from src.commands.gather_pipeline import channel_runtime
from src.core.channel_registry_store import ChannelRegistryStore
from src.core.exceptions import AuthError, QueryError
from src.core.failure_taxonomy import classify_exception
from src.core.gather_run_manifest import resolve_latest_committed_manifest
from src.core.gather_state_store import load_gather_state
from src.core.icm_signal_extractor import IcMSignalExtractor
from src.core.integration_types import (
    ChannelBinding,
    ChannelConfig,
    ChannelRegistration,
    DiscoveryCompleteness,
    RegistrationStatus,
    RunContext,
    ScopeStatusKind,
)
from src.core.journal import read_signals
from src.core.knowledge_store import KnowledgeStore
from src.core.models_v2 import ADOConfig, Program, Workstream
from src.core.program_paths import get_channel_registry_path
from src.core.reality_store import RealityStore
from src.m365 import icm_client
from src.m365.icm_discovery import IcMDiscoveryConfig, IcMDiscoveryProvider
from src.m365.icm_hydration import IcMHydrationConfig, IcMHydrationProvider


_NOW = datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc)
_PROGRAM = "synthetic-uil-icm-program"
_TEAM = "synthetic-team"
_INCIDENT = "123456789"
_PRIVATE = "SYNTHETICPRIVATEPROVIDERDETAIL"
_RUN = RunContext(force_discovery=True)


def _registration() -> ChannelRegistration:
    return ChannelRegistration(
        channel="icm",
        program_id=_PROGRAM,
        provider_instance_id="default",
        ref_id=_INCIDENT,
        ref_kind="incident",
        status=RegistrationStatus.ACTIVE,
        first_discovered_at=_NOW,
        last_seen_at=_NOW,
    )


@pytest.fixture
def scenario(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    root = tmp_path / "programs"
    db_root = tmp_path / "db"
    monkeypatch.setenv("VERTEX_DB_PATH", str(db_root))
    program = Program(
        schema_version="2.0",
        id=_PROGRAM,
        name="Synthetic",
        ado=ADOConfig(
            organization="example",
            project="Example",
            area_paths=("Example\\Area",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
        ),
    )
    workstreams = (Workstream(id="ws", name="Example", area_paths=("Example\\Area",)),)
    state = SimpleNamespace(
        phase="success", calls=[], requested=[], ids=(_INCIDENT,), failed_ids=(),
        error=QueryError(f"429 {_PRIVATE}"),
    )

    class Client:
        def __init__(self) -> None:
            state.calls.append("init")
            initialization = state.calls.count("init")
            if (state.phase == "discovery_init" and initialization == 1) or (
                state.phase == "hydration_init" and initialization == 2
            ):
                raise state.error

        def list_incidents(self, *, params: dict[str, object]) -> dict[str, object]:
            hydration = "id" in params
            phase = "hydration_query" if hydration else "discovery_query"
            state.calls.append(phase)
            if hydration:
                state.requested.append(str(params["id"]))
            if state.phase == phase:
                raise state.error
            if hydration and state.phase == "mixed" and str(params["id"]) in state.failed_ids:
                raise state.error
            incident_ids = (str(params["id"]),) if hydration else state.ids
            return {
                "items": [
                    {
                        "id": incident_id,
                        "owningTeamName": _TEAM,
                        "title": "Synthetic incident",
                        "severity": 1,
                        "status": "Active",
                        "modifiedAt": "2026-05-10T08:00:00+00:00",
                    }
                    for incident_id in incident_ids
                ]
            }

    binding = ChannelBinding(
        config=ChannelConfig(channel="icm", enabled=True, discovery_threshold_hours=24, ttl_days=30),
        discovery_provider=IcMDiscoveryProvider(),
        discovery_config=IcMDiscoveryConfig(owning_teams=(_TEAM,)),
        hydration_provider=IcMHydrationProvider(),
        hydration_config=IcMHydrationConfig(),
        signal_extractor=IcMSignalExtractor(),
    )
    monkeypatch.setattr(icm_client, "IcmClient", Client)
    monkeypatch.setattr(channel_runtime, "load_arch_data_fix", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(gather, "_load_program_context", lambda *_args, **_kwargs: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda *_args, **_kwargs: (14, 30))
    monkeypatch.setattr(
        gather,
        "load_program_knowledge",
        lambda *_args, **_kwargs: KnowledgeStore(
            people_directory=(), people_profiles=(), teams=(), products=(), golden_queries=()
        ),
    )
    monkeypatch.setattr(
        gather,
        "_resolve_uil_channel_binding_for_gather",
        lambda _program, _workstreams, channel, **_kwargs: binding if channel == "icm" else None,
    )
    return SimpleNamespace(
        root=root, db_root=db_root, program=program, workstreams=workstreams, state=state, binding=binding
    )


def _gather(scenario: SimpleNamespace):
    return gather.gather_program(
        _PROGRAM,
        programs_root=scenario.root,
        as_of=_NOW,
        loader=lambda *_args, **_kwargs: ((), 0),
        freshness_loader=lambda *_args, **_kwargs: ((), 0),
        include_icm=True,
        force_discovery=True,
    )


@pytest.mark.parametrize(
    ("phase", "error", "status", "retryable"),
    [
        ("discovery_init", AuthError(f"401 {_PRIVATE}"), ScopeStatusKind.AUTH_ERROR, False),
        ("discovery_init", RuntimeError(f"novel {_PRIVATE}"), ScopeStatusKind.AUTH_ERROR, False),
        ("discovery_query", AuthError(f"401 {_PRIVATE}"), ScopeStatusKind.AUTH_ERROR, True),
        ("discovery_query", QueryError(f"429 {_PRIVATE}"), ScopeStatusKind.ERROR, True),
        ("discovery_query", RuntimeError(f"novel {_PRIVATE}"), ScopeStatusKind.ERROR, True),
    ],
)
def test_discovery_provider_contains_caught_exception(
    scenario: SimpleNamespace, phase: str, error: Exception, status: ScopeStatusKind, retryable: bool
) -> None:
    scenario.state.phase = phase
    scenario.state.error = error
    result = IcMDiscoveryProvider().discover(
        _PROGRAM, IcMDiscoveryConfig(owning_teams=(_TEAM,)), (), run_ctx=_RUN
    )
    scope = result.scope_statuses["active_incidents"]
    assert scope.status is status and scope.completeness is DiscoveryCompleteness.PARTIAL
    assert len(result.errors) == 1 and result.errors[0].retryable is retryable
    assert result.errors[0].source == "icm" and result.errors[0].stage == "discovery"
    assert scope.error_message == result.errors[0].message
    assert _PRIVATE not in repr(result.errors) and _PRIVATE not in repr(scope)
    original = classify_exception(str(error))
    sanitized = classify_exception(result.errors[0].message)
    assert (sanitized.category, sanitized.retryable, sanitized.next_command) == (
        original.category, original.retryable, original.next_command
    )


@pytest.mark.parametrize("phase", ["hydration_init", "hydration_query"])
def test_hydration_provider_contains_caught_exception(scenario: SimpleNamespace, phase: str) -> None:
    scenario.state.phase = phase
    scenario.state.error = AuthError(f"401 {_PRIVATE}") if phase == "hydration_init" else QueryError(f"429 {_PRIVATE}")
    if phase == "hydration_init":
        scenario.state.calls.append("init")
    result = IcMHydrationProvider().hydrate(
        (_registration(),), _NOW, _PROGRAM, IcMHydrationConfig(), run_ctx=_RUN
    )
    assert result.failed_ref_ids == ((_INCIDENT, "incident"),)
    assert result.hydrated_ref_ids == ()
    assert len(result.errors) == 1
    error = result.errors[0]
    assert (error.source, error.stage, error.retryable) == ("icm", "hydration", phase == "hydration_query")
    assert _PRIVATE not in error.message
    original = (
        str(scenario.state.error)
        if phase == "hydration_init"
        else f"Failed to hydrate incident {_INCIDENT}: {scenario.state.error}"
    )
    before = classify_exception(original)
    after = classify_exception(error.message)
    assert (after.category, after.retryable, after.next_command) == (
        before.category, before.retryable, before.next_command
    )


@pytest.mark.parametrize("gather_v2", [False, True])
@pytest.mark.parametrize("phase", ["discovery_init", "discovery_query", "hydration_init", "hydration_query"])
def test_actual_gather_retains_failures_without_provider_text(
    scenario: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    phase: str, gather_v2: bool,
) -> None:
    monkeypatch.setattr(gather, "_gather_v2_enabled", lambda: gather_v2)
    scenario.state.phase = phase
    scenario.state.error = (
        AuthError(f"401 {_PROGRAM} {_PRIVATE}") if phase.endswith("init")
        else QueryError(f"429 {_PROGRAM} {_PRIVATE}")
    )
    with caplog.at_level(logging.WARNING):
        artifacts = _gather(scenario)
    state = load_gather_state(_PROGRAM, programs_root=scenario.root)
    manifest = resolve_latest_committed_manifest(_PROGRAM, programs_root=scenario.root)
    registry = ChannelRegistryStore(get_channel_registry_path(_PROGRAM, programs_root=scenario.root), _PROGRAM)
    deltas = registry.recent_deltas("icm")
    db_path = RealityStore(_PROGRAM, db_root=scenario.db_root).db_path
    with sqlite3.connect(db_path) as connection:
        ingestion = connection.execute(
            "SELECT status, error_message FROM reality_ingestion_runs WHERE source_ref = ?", ("icm",)
        ).fetchall()

    assert state is not None and manifest is not None and deltas
    assert artifacts.integration_error_count > 0
    assert gather._semantic_gather_exit_code(artifacts) == 2
    assert next(item for item in manifest.channel_outcomes if item.channel == "icm").degraded
    assert ingestion and all(row[0] == "failed" for row in ingestion)
    assert scenario.state.calls == {
        "discovery_init": ["init"],
        "discovery_query": ["init", "discovery_query"],
        "hydration_init": ["init", "discovery_query", "init"],
        "hydration_query": ["init", "discovery_query", "init", "hydration_query"],
    }[phase]
    assert any(item.stage == ("discovery" if phase.startswith("discovery") else "hydration")
               for item in artifacts.integration_errors)
    if phase.startswith("discovery"):
        assert deltas[0].failed_scopes["active_incidents"].completeness is DiscoveryCompleteness.PARTIAL
    surfaces = {
        "gather": repr(artifacts.integration_errors),
        "state": repr(state.integration_error_details),
        "manifest": repr(manifest.channel_outcomes),
        "registry": repr(deltas),
        "ingestion": repr(ingestion),
        "logs": caplog.text,
    }
    assert [name for name, value in surfaces.items() if _PRIVATE in value] == []
    assert all(_PROGRAM not in item.message for item in artifacts.integration_errors)


def test_successful_actual_gather_remains_non_degraded(scenario: SimpleNamespace) -> None:
    artifacts = _gather(scenario)
    assert artifacts.integration_error_count == 0
    assert gather._semantic_gather_exit_code(artifacts) == 0
    assert scenario.state.calls == ["init", "discovery_query", "init", "hydration_query"]
    assert any(signal.source == "icm" for signal in read_signals(_PROGRAM, programs_root=scenario.root))


@pytest.mark.parametrize("phase", ["discovery_query", "hydration_query"])
def test_actual_cli_reports_provider_failure_without_provider_text(
    scenario: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    scenario.state.phase = phase
    monkeypatch.setattr(gather, "PROGRAMS_ROOT", scenario.root)
    monkeypatch.setattr(gather, "_surface_alert_banner", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(gather, "_load_ado_items_via_uil", lambda *_args, **_kwargs: ((), (), 0))
    monkeypatch.setattr(
        gather, "_resolve_uil_channel_binding_for_gather", lambda *_args, **_kwargs: scenario.binding
    )
    result = CliRunner().invoke(app, ["gather", "--program", _PROGRAM, "--icm", "--force-discovery"])
    assert result.exit_code == 2, result.output
    assert f"icm/{phase.split('_')[0]}" in result.output and "Next:" in result.output
    assert _PRIVATE not in result.output


def test_observability_classification_remains_actionable(scenario: SimpleNamespace) -> None:
    scenario.state.phase = "discovery_query"
    scenario.state.error = QueryError(f"429 {_PRIVATE}")
    _gather(scenario)
    report = build_diagnose_report(_PROGRAM, programs_root=scenario.root)
    expected = classify_exception(str(scenario.state.error))
    assert (report.last_failure_category, report.last_failure_retryable, report.last_failure_next_command) == (
        expected.category, expected.retryable, expected.next_command
    )
    assert _PRIVATE not in repr(report)


@pytest.mark.parametrize("phase", ["discovery_init", "discovery_query", "hydration_init", "hydration_query"])
def test_every_existing_taxonomy_pattern_is_preserved(
    scenario: SimpleNamespace, phase: str,
) -> None:
    from src.core.failure_taxonomy import _PATTERN_TABLE

    texts = [pattern for _, patterns, _ in _PATTERN_TABLE for pattern in patterns]
    texts.extend(("novel", "401 and 429"))
    for text in texts:
        scenario.state.phase = phase
        scenario.state.calls.clear()
        scenario.state.error = (
            AuthError(f"{text} {_PRIVATE}") if phase.endswith("init")
            else QueryError(f"{text} {_PRIVATE}")
        )
        if phase.startswith("discovery"):
            discovery = IcMDiscoveryProvider().discover(
                _PROGRAM, IcMDiscoveryConfig(owning_teams=(_TEAM,)), (), run_ctx=_RUN
            )
            message = discovery.errors[0].message
            original = str(scenario.state.error)
        else:
            if phase == "hydration_init":
                scenario.state.calls.append("init")
            hydration = IcMHydrationProvider().hydrate(
                (_registration(),), _NOW, _PROGRAM, IcMHydrationConfig(), run_ctx=_RUN
            )
            message = hydration.errors[0].message
            original = (
                str(scenario.state.error)
                if phase == "hydration_init"
                else f"Failed to hydrate incident {_INCIDENT}: {scenario.state.error}"
            )
        expected = classify_exception(original)
        actual = classify_exception(message)
        assert _PRIVATE not in message
        assert (actual.category, actual.retryable, actual.next_command) == (
            expected.category, expected.retryable, expected.next_command
        )


def test_incident_reference_preserves_taxonomy_priority(scenario: SimpleNamespace) -> None:
    scenario.state.phase = "hydration_query"
    scenario.state.error = QueryError(f"401 {_PRIVATE}")
    registration = replace(_registration(), ref_id="429")
    result = IcMHydrationProvider().hydrate(
        (registration,), _NOW, _PROGRAM, IcMHydrationConfig(), run_ctx=_RUN
    )
    original = classify_exception(f"Failed to hydrate incident 429: {scenario.state.error}")
    actual = classify_exception(result.errors[0].message)
    assert result.failed_ref_ids == (("429", "incident"),)
    assert "429" in result.errors[0].message and _PRIVATE not in result.errors[0].message
    assert (actual.category, actual.retryable, actual.next_command) == (
        original.category, original.retryable, original.next_command
    )


def test_empty_provider_inputs_do_not_call_the_client(scenario: SimpleNamespace) -> None:
    discovery = IcMDiscoveryProvider().discover(
        _PROGRAM, IcMDiscoveryConfig(owning_teams=()), (), run_ctx=_RUN
    )
    hydration = IcMHydrationProvider().hydrate(
        (replace(_registration(), ref_id=_TEAM, ref_kind="icm_team"),),
        _NOW, _PROGRAM, IcMHydrationConfig(), run_ctx=_RUN,
    )
    assert discovery.errors == () and hydration.errors == ()
    assert discovery.scope_statuses["active_incidents"].status is ScopeStatusKind.SUCCESS
    assert hydration.hydrated_ref_ids == () and hydration.failed_ref_ids == ()
    assert scenario.state.calls == []


@pytest.mark.parametrize("phase", ["hydration_init", "hydration_query"])
def test_unexpected_hydration_errors_still_propagate(scenario: SimpleNamespace, phase: str) -> None:
    scenario.state.phase = phase
    scenario.state.error = TypeError("synthetic unexpected defect")
    with pytest.raises(TypeError, match="synthetic unexpected defect"):
        _gather(scenario)


@pytest.mark.parametrize("gather_v2", [False, True])
def test_mixed_hydration_preserves_usable_signal_and_distinct_failed_refs(
    scenario: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, gather_v2: bool,
) -> None:
    monkeypatch.setattr(gather, "_gather_v2_enabled", lambda: gather_v2)
    scenario.state.phase = "mixed"
    scenario.state.ids = ("10101", "20202", "30303")
    scenario.state.failed_ids = ("10101", "20202")
    artifacts = _gather(scenario)
    errors = tuple(error for error in artifacts.integration_errors if error.stage == "hydration")
    provider_errors = tuple(error for error in errors if error.message.startswith("Failed to hydrate incident "))
    signals = tuple(read_signals(_PROGRAM, programs_root=scenario.root))

    assert len(provider_errors) == 2
    assert all(any(ref in error.message for error in provider_errors) for ref in scenario.state.failed_ids)
    assert all(error.retryable and _PRIVATE not in error.message for error in provider_errors)
    assert set(scenario.state.requested) == set(scenario.state.ids)
    assert any(signal.source == "icm" and (signal.metadata or {}).get("incident_id") == "30303"
               for signal in signals)
    assert gather._semantic_gather_exit_code(artifacts) == 2
