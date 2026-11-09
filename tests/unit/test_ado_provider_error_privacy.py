from __future__ import annotations

import logging
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from cli import app
from src.commands import gather
from src.commands.doctor_checks.observability_checks import build_diagnose_report
from src.commands.gather_pipeline import channel_runtime
from src.core.ado_discovery import ADODiscoveryConfig, ADODiscoveryProvider
from src.core.ado_hydration import ADOHydrationConfig, ADOHydrationProvider
from src.core.ado_signal_extractor import ADOSignalExtractor
from src.core.channel_execution_policy import ChannelExecutionPolicy
from src.core.channel_registry_store import ChannelRegistryStore
from src.core.exceptions import QueryError
from src.core.failure_taxonomy import _PATTERN_TABLE, classify_exception
from src.core.gather_run_manifest import resolve_latest_committed_manifest
from src.core.gather_state_store import load_gather_state
from src.core.integration_types import (
    ChannelBinding,
    ChannelConfig,
    ChannelRegistration,
    DiscoveredRef,
    DiscoveryCompleteness,
    DiscoveryResult,
    RegistrationBinding,
    RegistrationStatus,
    ScopeStatus,
    ScopeStatusKind,
)
from src.core.journal import read_signals
from src.core.program_paths import get_channel_registry_path
from src.core.reality_store import RealityStore
from src.core.slice_contract_loader import (
    SliceAdoSourceContract,
    SliceContract,
    SliceDegradation,
    SliceFreshness,
    SliceOwners,
    SliceSourceContract,
)
from tests.unit.test_ado_hydration import _FakeADOClient
from tests.unit.test_icm_uil_provider_privacy import _NOW, _PRIVATE, _PROGRAM, scenario


class _Client(_FakeADOClient):
    def __init__(self) -> None:
        super().__init__()
        self.phase = "success"
        self.error: Exception = QueryError(f"429 {_PROGRAM} {_PRIVATE}")
        self.failed_ids = {101}
        self.calls: list[str] = []

    def get_saved_query(self, query_id: str) -> dict[str, object]:
        self.calls.append(f"saved:{query_id}")
        if self.phase == "discovery_saved" and query_id == "bad":
            raise self.error
        return {"id": query_id, "wiql": f"Select [System.Id] From WorkItems WHERE query = '{query_id}'"}

    def execute_wiql(self, wiql: str, top: int | None = None, *, on_pagination: Any = None) -> list[int]:
        self.calls.append("wiql:bad" if "'bad'" in wiql else "wiql:good")
        if self.phase == "discovery_query" and "'bad'" in wiql:
            raise self.error
        return [101]

    def query_work_items_batch(
        self, work_item_ids: list[int], fields: tuple[str, ...]
    ) -> list[dict[str, object]]:
        self.calls.append("batch")
        if self.phase == "batch":
            raise self.error
        return super().query_work_items_batch(work_item_ids, fields)

    def list_work_item_revisions(self, work_item_id: int, **kwargs: object) -> list[dict[str, object]]:
        self.calls.append(f"revisions:{work_item_id}")
        if self.phase == "detail" and work_item_id in self.failed_ids:
            raise self.error
        return super().list_work_item_revisions(work_item_id, **kwargs)

    def list_work_item_comments(self, work_item_id: int, **kwargs: object) -> list[dict[str, object]]:
        self.calls.append(f"comments:{work_item_id}")
        return super().list_work_item_comments(work_item_id, **kwargs)

    def get_work_item_relations(self, work_item_ids: list[int]) -> list[dict[str, object]]:
        self.calls.append("relations")
        if self.phase == "relations":
            raise self.error
        return []


@pytest.fixture
def ado(scenario: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    client = _Client()
    contract = SliceContract(
        id="synthetic.slice",
        scorecard_name="Synthetic",
        section="demo",
        workstream="Synthetic",
        slice_kind="scorecard_dimension",
        title="Synthetic",
        source_of_truth="ado_primary",
        owners=SliceOwners(primary="Synthetic"),
        source_contract=SliceSourceContract(
            ado=SliceAdoSourceContract(
                saved_queries=("bad", "good"),
                filters=None,
                explicit_work_item_ids=(),
                required_fields=(),
            ),
            telemetry=None,
            fallback_sources=(),
        ),
        freshness=SliceFreshness(warn_days=5, block_days=10),
        degradation=SliceDegradation(blank_filter_is_error=True),
        remediation_template=None,
    )
    binding = ChannelBinding(
        config=ChannelConfig(channel="ado", enabled=True, discovery_threshold_hours=24, ttl_days=30),
        discovery_provider=ADODiscoveryProvider(client),  # type: ignore[arg-type]
        discovery_config=ADODiscoveryConfig((contract,)),
        hydration_provider=ADOHydrationProvider(client),  # type: ignore[arg-type]
        hydration_config=ADOHydrationConfig(),
        signal_extractor=ADOSignalExtractor(),
    )
    monkeypatch.setattr(
        gather,
        "_resolve_uil_channel_binding_for_gather",
        lambda _program, _workstreams, channel, **_kwargs: binding if channel == "ado" else None,
    )
    return SimpleNamespace(scenario=scenario, client=client, binding=binding)


def _registration(ref_kind: str = "work_item", ref_id: str = "101") -> ChannelRegistration:
    return ChannelRegistration(
        channel="ado",
        program_id=_PROGRAM,
        provider_instance_id="default",
        ref_id=ref_id,
        ref_kind=ref_kind,
        status=RegistrationStatus.ACTIVE,
        first_discovered_at=_NOW,
        last_seen_at=_NOW,
    )


def _enable_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    policy = ChannelExecutionPolicy(
        channel="ado",
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


def _failure(
    ado: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, phase: str, ref_id: str = "101"
) -> tuple[Any, str]:
    client, binding = ado.client, ado.binding
    client.phase = phase
    if phase.startswith("discovery"):
        result = binding.discovery_provider.discover(_PROGRAM, binding.discovery_config, ())
        return result, result.scope_statuses["synthetic.slice:bad"].error_message
    if phase == "pull_requests":
        def fail(*_args: Any, **_kwargs: Any) -> None:
            raise client.error

        monkeypatch.setattr("src.core.ado_pr_client.ADOPRClient.list_pull_requests", fail)
        registration = _registration("repository", ref_id)
    else:
        registration = _registration(ref_id=ref_id)
    result = binding.hydration_provider.hydrate((registration,), _NOW, _PROGRAM, binding.hydration_config)
    return result, result.errors[0].message


_PHASES = ("discovery_saved", "discovery_query", "batch", "detail", "relations", "pull_requests")


@pytest.mark.parametrize("phase", _PHASES)
def test_provider_caught_error_is_private_and_retains_failure(
    ado: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    ref_id = "synthetic-repo" if phase == "pull_requests" else "101"
    result, diagnostic = _failure(ado, monkeypatch, phase, ref_id)
    if phase.startswith("discovery"):
        failed = result.scope_statuses["synthetic.slice:bad"]
        assert failed.status is ScopeStatusKind.ERROR and failed.completeness is DiscoveryCompleteness.PARTIAL
        assert result.completeness is DiscoveryCompleteness.PARTIAL
        assert result.scope_statuses["synthetic.slice:good"].status is ScopeStatusKind.SUCCESS
        assert [ref.registration.ref_id for ref in result.discovered_refs] == ["101"]
        assert [query.completeness_state for query in result.query_results] == ["ERROR", "FULL"]
        assert result.errors == ()
    else:
        error = result.errors[0]
        assert (error.source, error.stage, error.retryable) == ("ado", "hydration", True)
        if phase in ("batch", "detail", "pull_requests"):
            assert result.failed_ref_ids == ((ref_id, "repository" if phase == "pull_requests" else "work_item"),)
        else:
            assert result.hydrated_ref_ids == (("101", "work_item"),)
            assert result.failed_ref_ids == () and result.resources.work_items[0].id == 101
        if phase in ("detail", "pull_requests"):
            assert error.ref_id == ref_id
            assert error.ref_kind == ("repository" if phase == "pull_requests" else "work_item")
        assert result.api_call_count == {"batch": 1, "detail": 2, "relations": 3, "pull_requests": 0}[phase]
        assert error.operator_action == (
            "Verify Azure DevOps access and retry hydration." if phase == "batch" else None
        )
    assert _PRIVATE not in diagnostic and _PROGRAM not in diagnostic


@pytest.mark.parametrize("phase", _PHASES)
def test_every_taxonomy_pattern_remains_classified(
    ado: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    ref_id = "synthetic-repo" if phase == "pull_requests" else "101"
    prefix = {
        "detail": f"Failed to hydrate work item {ref_id}: ",
        "relations": "Failed to hydrate work item relations: ",
        "pull_requests": f"Failed to fetch pull requests for repository {ref_id}: ",
    }.get(phase, "")
    texts = [pattern for _, patterns, _ in _PATTERN_TABLE for pattern in patterns]
    texts.extend(("novel", "401 and 429"))
    for text in texts:
        ado.client.error = QueryError(f"{text} {_PRIVATE}")
        _, message = _failure(ado, monkeypatch, phase, ref_id)
        before = classify_exception(f"{prefix}{ado.client.error}")
        after = classify_exception(message)
        assert _PRIVATE not in message
        assert (after.category, after.retryable, after.next_command, after.detail) == (
            before.category, before.retryable, before.next_command, before.detail
        )


@pytest.mark.parametrize("phase", ["detail", "pull_requests"])
def test_reference_that_matches_taxonomy_retains_priority(
    ado: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    ado.client.error = QueryError(f"401 {_PRIVATE}")
    ado.client.failed_ids = {429}
    result, message = _failure(ado, monkeypatch, phase, "429")
    prefix = (
        "Failed to hydrate work item 429: " if phase == "detail"
        else "Failed to fetch pull requests for repository 429: "
    )
    assert result.errors[0].ref_id == "429"
    assert _PRIVATE not in message and "429" in message
    assert classify_exception(message) == classify_exception(f"{prefix}{ado.client.error}")


def test_shared_failed_query_preserves_each_scope(ado: SimpleNamespace) -> None:
    config = ado.binding.discovery_config
    contract = config.slice_contracts[0]
    ado.client.phase = "discovery_saved"
    result = ado.binding.discovery_provider.discover(
        _PROGRAM, replace(config, slice_contracts=(contract, replace(contract, id="second.slice"))), ()
    )
    for name in ("synthetic.slice:bad", "second.slice:bad"):
        assert result.scope_statuses[name].status is ScopeStatusKind.ERROR
        assert _PRIVATE not in result.scope_statuses[name].error_message
    assert len(result.discovered_refs) == 1
    assert ado.client.calls.count("saved:bad") == 1


@pytest.mark.parametrize("phase", ["discovery_saved", "discovery_query", "batch", "detail", "relations"])
@pytest.mark.parametrize("gather_v2", [False, True])
@pytest.mark.parametrize("budgeted", [False, True])
def test_actual_gather_surfaces_omit_caught_exception(
    ado: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    phase: str, gather_v2: bool, budgeted: bool,
) -> None:
    monkeypatch.setattr(gather, "_gather_v2_enabled", lambda: gather_v2)
    if budgeted:
        _enable_budget(monkeypatch)
    ado.client.phase = phase
    scenario = ado.scenario
    with caplog.at_level(logging.WARNING):
        artifacts = gather.gather_program(
            _PROGRAM, programs_root=scenario.root, as_of=_NOW, force_discovery=True
        )
    state = load_gather_state(_PROGRAM, programs_root=scenario.root)
    manifest = resolve_latest_committed_manifest(_PROGRAM, programs_root=scenario.root)
    report = build_diagnose_report(_PROGRAM, programs_root=scenario.root)
    registry = ChannelRegistryStore(get_channel_registry_path(_PROGRAM, programs_root=scenario.root), _PROGRAM)
    deltas = registry.recent_deltas("ado")
    with sqlite3.connect(RealityStore(_PROGRAM, db_root=scenario.db_root).db_path) as connection:
        ingestion = connection.execute(
            "SELECT source_ref, status, error_message FROM reality_ingestion_runs"
        ).fetchall()
    assert state is not None and manifest is not None and deltas and ingestion
    outcome = next(item for item in manifest.channel_outcomes if item.channel == "ado")
    assert outcome.degraded
    if phase.startswith("discovery"):
        assert deltas[0].failed_scopes["synthetic.slice:bad"].status is ScopeStatusKind.ERROR
        assert "batch" in ado.client.calls and "revisions:101" in ado.client.calls
        assert any(query.completeness_state == "ERROR" for query in artifacts.ado_query_results)
        assert gather._semantic_gather_exit_code(artifacts) == 3
    else:
        assert any(item.stage == "hydration" and item.source == "ado" for item in artifacts.integration_errors)
        assert gather._semantic_gather_exit_code(artifacts) == 3
        expected = classify_exception(str(ado.client.error))
        provider_error = next(item for item in artifacts.integration_errors if "matched pattern:" in item.message)
        assert classify_exception(provider_error.message) == expected
        first = classify_exception(state.integration_error_details[0].message)
        assert (report.last_failure_category, report.last_failure_retryable, report.last_failure_next_command) == (
            first.category, first.retryable, first.next_command
        )
    surfaces = {
        "result": repr(artifacts.integration_errors),
        "state": repr(state.integration_error_details),
        "manifest": repr(manifest.channel_outcomes),
        "registry": repr(deltas),
        "signals": repr(read_signals(_PROGRAM, programs_root=scenario.root)),
        "ingestion": repr(ingestion),
        "observability": repr(report),
        "logs": caplog.text,
    }
    assert [name for name, value in surfaces.items() if _PRIVATE in value] == []
    assert _PROGRAM not in (outcome.degrade_reason or "")


@pytest.mark.parametrize("phase", ["batch", "detail", "relations"])
def test_actual_cli_reports_hydration_failure_without_provider_text(
    ado: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    ado.client.phase = phase
    monkeypatch.setattr(gather, "PROGRAMS_ROOT", ado.scenario.root)
    monkeypatch.setattr(gather, "_surface_alert_banner", lambda *_args, **_kwargs: None)
    result = CliRunner().invoke(app, ["gather", "--program", _PROGRAM, "--force-discovery"])
    assert result.exit_code == 3, result.output
    assert "ado/hydration" in result.output and "Next:" in result.output
    assert _PRIVATE not in result.output


@pytest.mark.parametrize("gather_v2", [False, True])
@pytest.mark.parametrize("budgeted", [False, True])
def test_actual_gather_contains_pull_request_exception(
    ado: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, gather_v2: bool, budgeted: bool
) -> None:
    now = datetime.now(timezone.utc)
    registration = replace(_registration("repository", "synthetic-repo"), first_discovered_at=now, last_seen_at=now)
    binding = RegistrationBinding(
        workstream_id="ws", scope_id="manual", source_type="manual_config",
        confidence=1.0, confidence_source="manual_config",
    )
    store = ChannelRegistryStore(get_channel_registry_path(_PROGRAM, programs_root=ado.scenario.root), _PROGRAM)
    store.apply_discovery_result(DiscoveryResult(
        channel="ado", program_id=_PROGRAM, discovered_refs=(DiscoveredRef(registration, (binding,)),),
        completeness=DiscoveryCompleteness.FULL,
        scope_statuses={"manual": ScopeStatus("manual", ScopeStatusKind.SUCCESS, DiscoveryCompleteness.FULL, 1)},
        scope_state_updates={}, errors=(), computed_at=now,
    ))
    monkeypatch.setattr(ChannelRegistryStore, "is_discovery_stale", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(gather, "_gather_v2_enabled", lambda: gather_v2)
    if budgeted:
        _enable_budget(monkeypatch)

    def fail(*_args: Any, **_kwargs: Any) -> None:
        raise ado.client.error

    monkeypatch.setattr("src.core.ado_pr_client.ADOPRClient.list_pull_requests", fail)
    artifacts = gather.gather_program(_PROGRAM, programs_root=ado.scenario.root, as_of=_NOW)
    state = load_gather_state(_PROGRAM, programs_root=ado.scenario.root)
    manifest = resolve_latest_committed_manifest(_PROGRAM, programs_root=ado.scenario.root)
    report = build_diagnose_report(_PROGRAM, programs_root=ado.scenario.root)
    assert state is not None and manifest is not None
    assert gather._semantic_gather_exit_code(artifacts) == 3
    assert any("synthetic-repo" in error.message for error in artifacts.integration_errors)
    outcome = next(item for item in manifest.channel_outcomes if item.channel == "ado")
    assert outcome.degraded
    assert _PRIVATE not in repr((artifacts.integration_errors, state.integration_error_details, outcome, report))


@pytest.mark.parametrize("parallel", [False, True])
def test_detail_failure_keeps_independent_work_item(
    ado: SimpleNamespace, parallel: bool
) -> None:
    ado.client.phase = "detail"
    factory = (lambda: ado.client) if parallel else None
    provider = ADOHydrationProvider(ado.client, detail_client_factory=factory)
    result = provider.hydrate(
        (_registration(ref_id="101"), _registration(ref_id="102")),
        _NOW, _PROGRAM, ADOHydrationConfig(detail_max_workers=2),
    )
    assert result.failed_ref_ids == (("101", "work_item"),)
    assert result.hydrated_ref_ids == (("102", "work_item"),)
    assert [item.id for item in result.resources.work_items] == [102]
    assert result.errors[0].ref_id == "101" and _PRIVATE not in result.errors[0].message


def test_multiple_failed_items_keep_distinct_diagnostics(ado: SimpleNamespace) -> None:
    ado.client.phase = "detail"
    ado.client.failed_ids = {101, 102}
    result = ado.binding.hydration_provider.hydrate(
        (_registration(ref_id="101"), _registration(ref_id="102")),
        _NOW, _PROGRAM, ado.binding.hydration_config,
    )
    assert result.failed_ref_ids == (("101", "work_item"), ("102", "work_item"))
    assert [error.ref_id for error in result.errors] == ["101", "102"]
    assert len({error.message for error in result.errors}) == 2
    assert all(_PRIVATE not in error.message for error in result.errors)


def test_pull_request_failure_keeps_independent_repository(
    ado: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fetch(_self: Any, repo_id: str, *, top: int) -> tuple[()]:
        assert top == 100
        if repo_id == "bad-repo":
            raise ado.client.error
        return ()

    monkeypatch.setattr("src.core.ado_pr_client.ADOPRClient.list_pull_requests", fetch)
    result = ado.binding.hydration_provider.hydrate(
        (_registration("repository", "bad-repo"), _registration("repository", "good-repo")),
        _NOW, _PROGRAM, ado.binding.hydration_config,
    )
    assert result.failed_ref_ids == (("bad-repo", "repository"),)
    assert result.hydrated_ref_ids == (("good-repo", "repository"),)
    assert result.api_call_count == 1
    assert result.errors[0].ref_id == "bad-repo" and _PRIVATE not in result.errors[0].message


@pytest.mark.parametrize("error_type", [ValueError, OSError])
def test_pull_request_handler_retains_existing_exception_scope(
    ado: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, error_type: type[Exception]
) -> None:
    ado.client.error = error_type(f"401 {_PRIVATE}")
    result, message = _failure(ado, monkeypatch, "pull_requests", "synthetic-repo")
    assert result.failed_ref_ids == (("synthetic-repo", "repository"),)
    assert _PRIVATE not in message and classify_exception(message) == classify_exception(
        f"Failed to fetch pull requests for repository synthetic-repo: {ado.client.error}"
    )


def test_pull_request_handler_does_not_catch_base_exception(
    ado: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupt(*_args: Any, **_kwargs: Any) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("src.core.ado_pr_client.ADOPRClient.list_pull_requests", interrupt)
    with pytest.raises(KeyboardInterrupt):
        ado.binding.hydration_provider.hydrate(
            (_registration("repository", "synthetic-repo"),), _NOW, _PROGRAM, ado.binding.hydration_config
        )


def test_empty_hydration_does_not_call_client(ado: SimpleNamespace) -> None:
    result = ado.binding.hydration_provider.hydrate((), _NOW, _PROGRAM, ado.binding.hydration_config)
    assert result.errors == () and result.hydrated_ref_ids == () and result.failed_ref_ids == ()
    assert ado.client.calls == []


@pytest.mark.parametrize("phase", ["discovery_saved", "discovery_query", "batch", "detail", "relations"])
def test_unexpected_provider_exception_still_propagates(
    ado: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    ado.client.error = ValueError("synthetic unexpected error")
    with pytest.raises(ValueError, match="synthetic unexpected error"):
        _failure(ado, monkeypatch, phase)


def test_successful_ado_gather_remains_non_degraded(ado: SimpleNamespace) -> None:
    artifacts = gather.gather_program(
        _PROGRAM, programs_root=ado.scenario.root, as_of=_NOW, force_discovery=True
    )
    manifest = resolve_latest_committed_manifest(_PROGRAM, programs_root=ado.scenario.root)
    assert artifacts.integration_error_count == 0
    assert manifest is not None
    assert not next(item for item in manifest.channel_outcomes if item.channel == "ado").degraded
