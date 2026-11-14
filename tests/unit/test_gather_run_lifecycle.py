"""D-13/Sec 4.6: tests for the gather-run manifest lifecycle wiring into
``gather_program`` (the lease-acquire / staging-manifest-create / delegate /
commit-or-fail / lease-release wrapper around the existing gather
implementation).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3

import pytest
import typer

from src.commands import gather
from src.core.alerts import append_or_suppress_alert
from src.core.gather_run_manifest import (
    GATHER_MUTATION_DOMAIN,
    GatherRunManifest,
    GatherRunStatus,
    RequiredScopeStatus,
    create_staging_manifest,
    get_committed_run_dir,
    get_quarantine_run_dir,
    get_staging_run_dir,
    read_manifest,
    resolve_latest_committed_manifest,
    resolve_latest_full_committed_manifest,
    ORACLE_RESULT_OPERATOR_EXPORT_MATCH,
    ORACLE_RESULT_SAME_ENDPOINT_RERUN,
)
from src.core.ledger.ulid import new_ulid
from src.core.models import RiskLevel, WorkItem
from src.core.models_v2 import ADOConfig, Program, Workstream
from src.core.models_v2 import IntegrationError
from src.core.integration_types import DiscoveryQueryResult
from src.commands.gather_pipeline.models import GatherArtifacts
from src.commands.gather_pipeline.lifecycle_policy import GatherRuntimePolicy
from src.core.workspace_lease import LeaseFencingTokenStale, acquire_lease


def _fixture_program() -> tuple[Program, tuple[Workstream, ...]]:
    program = Program(
        schema_version="2.0",
        id="acme",
        name="Acme",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Acme",
            area_paths=("One\\Adventure\\Acme",),
            dri_email="maintainer@example.com",
        ),
    )
    return program, workstreams


def _fixture_item() -> WorkItem:
    return WorkItem(
        id=1234,
        type="Feature",
        title="Ramp checkpoint",
        state="Active",
        assigned_to="Priya",
        assigned_to_email="priya@example.com",
        area_path="One\\Adventure\\Acme",
        iteration_path="One\\FY26\\Q4",
        target_date=None,
        risk_level=RiskLevel.MEDIUM,
        tags=["acme"],
        custom_fields={},
        revisions=[],
        comments=[],
        fetched_at=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
    )


@pytest.mark.parametrize("gather_v2", [False, True])
@pytest.mark.parametrize("query_fails", [False, True])
@pytest.mark.parametrize("pagination_verified", [False, True])
def test_real_uil_discovery_outcome_reaches_committed_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, gather_v2: bool, query_fails: bool,
    pagination_verified: bool,
) -> None:
    from types import SimpleNamespace

    from src.commands.gather_pipeline import channel_runtime
    from src.core.ado_discovery import ADODiscoveryConfig, ADODiscoveryProvider
    from src.core.exceptions import QueryError
    from src.core.integration_types import ADOHydrationOutput, ChannelConfig, HydrationResult, PaginationOutcome
    from src.core.slice_contract_loader import load_slice_contract

    programs_root = tmp_path / "programs"
    program, workstreams = _fixture_program()
    program_dir = programs_root / program.id
    program_dir.mkdir(parents=True)
    contract_path = program_dir / "slice_contracts.yaml"
    contract_path.write_text(
        'schema_version: "1.0"\nslices:\n'
        "  - id: acme\n    scorecard_name: Acme\n    section: acme\n"
        "    workstream: Acme\n    slice_kind: scorecard_dimension\n    title: Delivery\n"
        "    source_of_truth: ado_primary\n    owners:\n      primary: Owner\n"
        "    source_contract:\n      ado:\n        saved_queries: [good, failed]\n"
        "        tag_expression:\n          all_of: [Release]\n        required_fields: [state]\n"
        "    freshness:\n      warn_days: 5\n      block_days: 10\n"
        "    degradation:\n      blank_filter_is_error: true\n",
        encoding="utf-8",
    )

    baseline = not pagination_verified

    class Client:
        def get_saved_query(self, query_id):
            if query_fails and not baseline and query_id == "failed":
                raise QueryError("Synthetic saved query unavailable")
            return {"id": query_id, "wiql": "Select [System.Id] From WorkItems"}

        def execute_wiql(self, wiql, top=None, **kwargs):
            if pagination_verified or baseline:
                kwargs["on_pagination"](PaginationOutcome(1, 1, False))
            return [1234]

    item = _fixture_item()
    binding = SimpleNamespace(
        config=ChannelConfig(channel="ado", enabled=True, discovery_threshold_hours=24, ttl_days=30),
        discovery_provider=ADODiscoveryProvider(Client()),
        discovery_config=ADODiscoveryConfig(load_slice_contract(contract_path)),
        hydration_provider=SimpleNamespace(
            hydrate=lambda *args, **kwargs: HydrationResult(
                channel="ado", resources=ADOHydrationOutput(work_items=(item,), freshness_items=(item,)),
                api_call_count=1, errors=(), hydrated_ref_ids=(("1234", "work_item"),), failed_ref_ids=(),
            ),
        ),
        hydration_config=object(),
    )
    monkeypatch.setattr(gather, "_load_program_context", lambda *args, **kwargs: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda *args, **kwargs: (14, 30))
    monkeypatch.setattr(
        gather, "_resolve_uil_channel_binding_for_gather",
        lambda program, workstreams, channel, **kwargs: binding if channel == "ado" else None,
    )
    monkeypatch.setattr(gather, "_gather_v2_enabled", lambda: gather_v2)
    monkeypatch.setattr(channel_runtime, "load_arch_data_fix", lambda *args, **kwargs: None)
    prior_full = None
    if baseline:
        gather.gather_program("acme", as_of=item.fetched_at, programs_root=programs_root, force_discovery=True)
        prior_full = resolve_latest_full_committed_manifest("acme", programs_root=programs_root)
        assert prior_full is not None
        baseline = False
        binding.discovery_provider = ADODiscoveryProvider(Client())
    artifacts = gather.gather_program(
        "acme", as_of=item.fetched_at, programs_root=programs_root, force_discovery=True,
    )

    committed = resolve_latest_committed_manifest("acme", programs_root=programs_root)
    assert committed is not None
    assert artifacts.hydrated_work_item_ids == ("1234",)
    assert committed.hydrated_count == 1
    assert committed.discovered_count == 1
    ado_outcomes = tuple(outcome for outcome in committed.channel_outcomes if outcome.channel == "ado")
    assert len(ado_outcomes) == 1
    degraded = query_fails or not pagination_verified
    assert ado_outcomes[0].degraded is degraded
    assert bool(ado_outcomes[0].degrade_reason) is degraded
    assert committed.required_scope_status is (
        RequiredScopeStatus.PARTIAL if degraded else RequiredScopeStatus.FULL
    )
    assert any(result.completeness_state == "ERROR" for result in committed.query_results) is query_fails
    assert any(result.failure_category == "unverified_pagination" for result in committed.query_results) is (
        not pagination_verified
    )
    assert all(not result.cap_reached for result in committed.query_results)
    latest_full = resolve_latest_full_committed_manifest("acme", programs_root=programs_root)
    if prior_full is not None:
        assert latest_full is not None and latest_full.run_id == prior_full.run_id
    else:
        assert (latest_full is None) is degraded


@pytest.mark.parametrize("gather_v2", [False, True])
def test_partition_scan_partial_does_not_advance_prior_full(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, gather_v2: bool,
) -> None:
    from types import SimpleNamespace

    from src.commands.gather_pipeline import channel_runtime
    from src.core.ado_discovery import ADODiscoveryConfig, ADODiscoveryProvider
    from src.core.integration_types import ADOHydrationOutput, ChannelConfig, HydrationResult, PaginationOutcome
    from src.core.slice_contract_loader import load_slice_contract

    programs_root = tmp_path / "programs"
    program, workstreams = _fixture_program()
    program_dir = programs_root / program.id
    program_dir.mkdir(parents=True)
    contract_path = program_dir / "slice_contracts.yaml"
    contract_path.write_text(
        'schema_version: "1.0"\nslices:\n'
        "  - id: acme\n    scorecard_name: Acme\n    section: acme\n"
        "    workstream: Acme\n    slice_kind: scorecard_dimension\n    title: Delivery\n"
        "    source_of_truth: ado_primary\n    owners: {primary: Owner}\n"
        "    source_contract:\n      ado:\n        saved_queries: [capped]\n"
        "        tag_expression: {all_of: [Release]}\n        required_fields: [state]\n"
        "    freshness: {warn_days: 5, block_days: 10}\n"
        "    degradation: {blank_filter_is_error: true}\n",
        encoding="utf-8",
    )

    class Client:
        second_run = False

        def get_saved_query(self, query_id):
            return {"wiql": "Select [System.Id] From WorkItems"}

        def execute_wiql(self, wiql, top=None, *, on_pagination=None):
            if self.second_run and "[System.Id] > " in wiql:
                return [1234]
            ids = [1234, 9999] if self.second_run else [1234]
            if on_pagination is not None:
                on_pagination(PaginationOutcome(len(ids), 1, len(ids) == top))
            return ids

    client = Client()
    item = _fixture_item()
    binding = SimpleNamespace(
        config=ChannelConfig(channel="ado", enabled=True, discovery_threshold_hours=24, ttl_days=30),
        discovery_provider=ADODiscoveryProvider(client),
        discovery_config=ADODiscoveryConfig(load_slice_contract(contract_path), top=2),
        hydration_provider=SimpleNamespace(
            hydrate=lambda *args, **kwargs: HydrationResult(
                channel="ado", resources=ADOHydrationOutput(work_items=(item,), freshness_items=(item,)),
                api_call_count=1, errors=(), hydrated_ref_ids=(("1234", "work_item"),), failed_ref_ids=(),
            ),
        ),
        hydration_config=object(),
    )
    monkeypatch.setattr(gather, "_load_program_context", lambda *args, **kwargs: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda *args, **kwargs: (14, 30))
    monkeypatch.setattr(
        gather, "_resolve_uil_channel_binding_for_gather",
        lambda program, workstreams, channel, **kwargs: binding if channel == "ado" else None,
    )
    monkeypatch.setattr(gather, "_gather_v2_enabled", lambda: gather_v2)
    monkeypatch.setattr(channel_runtime, "load_arch_data_fix", lambda *args, **kwargs: None)

    gather.gather_program("acme", as_of=item.fetched_at, programs_root=programs_root, force_discovery=True)
    prior_full = resolve_latest_full_committed_manifest("acme", programs_root=programs_root)
    assert prior_full is not None and prior_full.required_scope_status is RequiredScopeStatus.FULL

    client.second_run = True
    gather.gather_program("acme", as_of=item.fetched_at, programs_root=programs_root, force_discovery=True)
    committed = resolve_latest_committed_manifest("acme", programs_root=programs_root)
    latest_full = resolve_latest_full_committed_manifest("acme", programs_root=programs_root)

    assert committed is not None and committed.required_scope_status is RequiredScopeStatus.PARTIAL
    assert committed.discovered_count == 2
    assert committed.query_results[0].cap_reached is True
    assert set(committed.query_results[0].membership_ids) == {"1234", "9999"}
    assert latest_full is not None and latest_full.run_id == prior_full.run_id


@pytest.mark.parametrize("gather_v2", [False, True])
@pytest.mark.parametrize("mode", ["initial", "partition"])
def test_malformed_wiql_does_not_advance_prior_full(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, gather_v2: bool, mode: str,
) -> None:
    from types import SimpleNamespace

    from src.commands.gather_pipeline import channel_runtime
    from src.core.ado_client import ADOClient
    from src.core.ado_discovery import ADODiscoveryConfig, ADODiscoveryProvider
    from src.core.integration_types import ADOHydrationOutput, ChannelConfig, HydrationResult
    from src.core.slice_contract_loader import load_slice_contract

    programs_root = tmp_path / "programs"
    program, workstreams = _fixture_program()
    program_dir = programs_root / program.id
    program_dir.mkdir(parents=True)
    contract_path = program_dir / "slice_contracts.yaml"
    contract_path.write_text(
        'schema_version: "1.0"\nslices:\n'
        "  - id: acme\n    scorecard_name: Acme\n    section: acme\n"
        "    workstream: Acme\n    slice_kind: scorecard_dimension\n    title: Delivery\n"
        "    source_of_truth: ado_primary\n    owners: {primary: Owner}\n"
        "    source_contract:\n      ado:\n        saved_queries: [capped]\n"
        "        tag_expression: {all_of: [Release]}\n        required_fields: [state]\n"
        "    freshness: {warn_days: 5, block_days: 10}\n"
        "    degradation: {blank_filter_is_error: true}\n",
        encoding="utf-8",
    )

    class Client(ADOClient):
        second_run = False

        def __init__(self) -> None:
            self._rest_base_url = "https://example.invalid/"

        def get_saved_query(self, query_id):
            return {"wiql": "Select [System.Id] From WorkItems"}

        def _request_json(self, method, url, **kwargs):
            wiql = kwargs["json"]["query"]
            if self.second_run and (mode == "initial" or "[System.Id] > " in wiql):
                return {"workItems": [{"id": 1234}, "private-value", {"id": 9999}]}
            ids = [1234, 9999] if self.second_run else [1234]
            return {"workItems": [{"id": item} for item in ids]}

    client = Client()
    item = _fixture_item()
    binding = SimpleNamespace(
        config=ChannelConfig(channel="ado", enabled=True, discovery_threshold_hours=24, ttl_days=30),
        discovery_provider=ADODiscoveryProvider(client),
        discovery_config=ADODiscoveryConfig(load_slice_contract(contract_path), top=2),
        hydration_provider=SimpleNamespace(
            hydrate=lambda *args, **kwargs: HydrationResult(
                channel="ado", resources=ADOHydrationOutput(work_items=(item,), freshness_items=(item,)),
                api_call_count=1, errors=(), hydrated_ref_ids=(("1234", "work_item"),), failed_ref_ids=(),
            ),
        ),
        hydration_config=object(),
    )
    monkeypatch.setattr(gather, "_load_program_context", lambda *args, **kwargs: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda *args, **kwargs: (14, 30))
    monkeypatch.setattr(
        gather, "_resolve_uil_channel_binding_for_gather",
        lambda program, workstreams, channel, **kwargs: binding if channel == "ado" else None,
    )
    monkeypatch.setattr(gather, "_gather_v2_enabled", lambda: gather_v2)
    monkeypatch.setattr(channel_runtime, "load_arch_data_fix", lambda *args, **kwargs: None)

    gather.gather_program("acme", as_of=item.fetched_at, programs_root=programs_root, force_discovery=True)
    prior_full = resolve_latest_full_committed_manifest("acme", programs_root=programs_root)
    assert prior_full is not None and prior_full.required_scope_status is RequiredScopeStatus.FULL

    client.second_run = True
    gather.gather_program("acme", as_of=item.fetched_at, programs_root=programs_root, force_discovery=True)
    committed = resolve_latest_committed_manifest("acme", programs_root=programs_root)
    latest_full = resolve_latest_full_committed_manifest("acme", programs_root=programs_root)

    assert committed is not None and committed.required_scope_status is RequiredScopeStatus.PARTIAL
    assert committed.discovered_count == 2
    assert committed.query_results[0].cap_reached is (mode == "partition")
    assert committed.query_results[0].completeness_state == "ERROR"
    assert set(committed.query_results[0].membership_ids) == {"1234", "9999"}
    assert latest_full is not None and latest_full.run_id == prior_full.run_id


@pytest.mark.parametrize("gather_v2", [False, True])
@pytest.mark.parametrize("failure", ["changed", "unavailable"])
def test_saved_query_refresh_does_not_advance_prior_full(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, gather_v2: bool, failure: str,
) -> None:
    from types import SimpleNamespace

    from src.commands.gather_pipeline import channel_runtime
    from src.core.ado_client import ADOClient
    from src.core.ado_discovery import ADODiscoveryConfig, ADODiscoveryProvider
    from src.core.exceptions import QueryError
    from src.core.integration_types import ADOHydrationOutput, ChannelConfig, HydrationResult
    from src.core.slice_contract_loader import load_slice_contract

    programs_root = tmp_path / "programs"
    program, workstreams = _fixture_program()
    program_dir = programs_root / program.id
    program_dir.mkdir(parents=True)
    contract_path = program_dir / "slice_contracts.yaml"
    contract_path.write_text(
        'schema_version: "1.0"\nslices:\n'
        "  - id: acme\n    scorecard_name: Acme\n    section: acme\n"
        "    workstream: Acme\n    slice_kind: scorecard_dimension\n    title: Delivery\n"
        "    source_of_truth: ado_primary\n    owners: {primary: Owner}\n"
        "    source_contract:\n      ado:\n        saved_queries: [saved]\n"
        "        tag_expression: {all_of: [Release]}\n        required_fields: [state]\n"
        "    freshness: {warn_days: 5, block_days: 10}\n"
        "    degradation: {blank_filter_is_error: true}\n",
        encoding="utf-8",
    )

    class Client(ADOClient):
        second_run = False

        def __init__(self) -> None:
            self._rest_base_url = "https://example.invalid/"
            self.lookups = 0
            self.executions = 0

        def get_saved_query(self, query_id):
            self.lookups += 1
            if self.second_run and failure == "unavailable":
                raise QueryError("synthetic private provider detail")
            suffix = " where [System.Id] >= 1" if self.second_run else ""
            return {"wiql": "Select [System.Id] From WorkItems" + suffix}

        def _request_json(self, method, url, **kwargs):
            self.executions += 1
            return {"workItems": [{"id": 1234}]}

    client = Client()
    item = _fixture_item()
    binding = SimpleNamespace(
        config=ChannelConfig(channel="ado", enabled=True, discovery_threshold_hours=24, ttl_days=30),
        discovery_provider=ADODiscoveryProvider(client),
        discovery_config=ADODiscoveryConfig(load_slice_contract(contract_path)),
        hydration_provider=SimpleNamespace(
            hydrate=lambda *args, **kwargs: HydrationResult(
                channel="ado", resources=ADOHydrationOutput(work_items=(item,), freshness_items=(item,)),
                api_call_count=1, errors=(), hydrated_ref_ids=(("1234", "work_item"),), failed_ref_ids=(),
            ),
        ),
        hydration_config=object(),
    )
    monkeypatch.setattr(gather, "_load_program_context", lambda *args, **kwargs: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda *args, **kwargs: (14, 30))
    monkeypatch.setattr(
        gather, "_resolve_uil_channel_binding_for_gather",
        lambda program, workstreams, channel, **kwargs: binding if channel == "ado" else None,
    )
    monkeypatch.setattr(gather, "_gather_v2_enabled", lambda: gather_v2)
    monkeypatch.setattr(channel_runtime, "load_arch_data_fix", lambda *args, **kwargs: None)

    gather.gather_program("acme", as_of=item.fetched_at, programs_root=programs_root, force_discovery=True)
    prior_full = resolve_latest_full_committed_manifest("acme", programs_root=programs_root)
    assert prior_full is not None and prior_full.required_scope_status is RequiredScopeStatus.FULL

    client.second_run = True
    gather.gather_program("acme", as_of=item.fetched_at, programs_root=programs_root, force_discovery=True)
    committed = resolve_latest_committed_manifest("acme", programs_root=programs_root)
    latest_full = resolve_latest_full_committed_manifest("acme", programs_root=programs_root)

    assert client.lookups == 2 and client.executions == 1
    assert committed is not None and committed.required_scope_status is RequiredScopeStatus.PARTIAL
    assert committed.query_results[0].failure_category == (
        "saved_query_changed" if failure == "changed" else "query_error"
    )
    assert committed.query_results[0].wiql_hash == ""
    assert latest_full is not None and latest_full.run_id == prior_full.run_id


def test_gather_program_commits_manifest_on_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program, workstreams = _fixture_program()
    item = _fixture_item()
    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))

    as_of = datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc)
    artifacts = gather.gather_program(
        "acme",
        as_of=as_of,
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of, **_: ((item,), 3),
    )

    committed = resolve_latest_committed_manifest("acme", programs_root=programs_root)
    assert committed is not None
    assert committed.status is GatherRunStatus.COMMITTED
    assert committed.program_id == "acme"
    # An injected legacy loader has hydrated items but no immutable query
    # membership, so it cannot establish authoritative discovery scope.
    assert committed.discovered_count == 0
    assert committed.hydrated_count == artifacts.scanned_items
    assert committed.ado_call_count == artifacts.ado_calls
    assert committed.manifest_hash is not None
    assert committed.lease_owner.startswith("vertex_gather:")

    assert committed.required_scope_status is RequiredScopeStatus.PARTIAL
    assert committed.last_successful_full_discovery_at is None
    assert committed.last_attempt_at is not None
    assert committed.next_expected_run_at is None
    assert committed.consecutive_failed_runs == 1
    assert committed.freshness_state == "block"
    assert resolve_latest_full_committed_manifest("acme", programs_root=programs_root) is None

    # The lease must be released so a subsequent gather can proceed.
    handle = acquire_lease("acme", "next-caller", mutation_domain=GATHER_MUTATION_DOMAIN, programs_root=programs_root)
    assert handle.owner == "next-caller"

    # No leftover staging directory for the completed run.
    assert not get_staging_run_dir("acme", committed.run_id, programs_root=programs_root).exists()


def _fixture_item_with_state_change() -> WorkItem:
    from src.core.models import Revision

    return WorkItem(
        id=1234,
        type="Feature",
        title="Ramp checkpoint",
        state="Active",
        assigned_to="Priya",
        assigned_to_email="priya@example.com",
        area_path="One\\Adventure\\Acme",
        iteration_path="One\\FY26\\Q4",
        target_date=None,
        risk_level=RiskLevel.MEDIUM,
        tags=["acme"],
        custom_fields={},
        revisions=[
            Revision(
                work_item_id=1234,
                rev_number=7,
                changed_by="priya@example.com",
                changed_by_email="priya@example.com",
                changed_date=datetime(2026, 5, 8, 9, 0, tzinfo=timezone.utc),
                fields_changed={"System.State": ("Proposed", "Active")},
            )
        ],
        comments=[],
        fetched_at=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
    )


def _real_uil_fixture(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, gather_v2: bool):
    from types import SimpleNamespace

    from src.commands.gather_pipeline import channel_runtime
    from src.core.ado_discovery import ADODiscoveryConfig, ADODiscoveryProvider
    from src.core.ado_hydration import ADOHydrationConfig, ADOHydrationProvider
    from src.core.integration_types import ChannelConfig, PaginationOutcome
    from src.core.slice_contract_loader import load_slice_contract

    programs_root = tmp_path / "programs"
    program, workstreams = _fixture_program()
    program_dir = programs_root / program.id
    program_dir.mkdir(parents=True)
    contract_path = program_dir / "slice_contracts.yaml"
    contract_path.write_text(
        'schema_version: "1.0"\nslices:\n'
        "  - id: acme\n    scorecard_name: Acme\n    section: acme\n"
        "    workstream: Acme\n    slice_kind: scorecard_dimension\n    title: Delivery\n"
        "    source_of_truth: ado_primary\n    owners: {primary: Owner}\n"
        "    source_contract:\n      ado:\n        saved_queries: [saved]\n"
        "        tag_expression: {all_of: [Release]}\n        required_fields: [state]\n"
        "    freshness: {warn_days: 5, block_days: 10}\n"
        "    degradation: {blank_filter_is_error: true}\n",
        encoding="utf-8",
    )

    class Client:
        changed_at = "2026-05-09T08:00:00Z"

        def __init__(self):
            self.revision_calls = []
            self.comment_calls = []
            self.ids = [1234]
            self.failed_comments = set()

        def get_saved_query(self, query_id):
            return {"wiql": "Select [System.Id] From WorkItems"}

        def execute_wiql(self, wiql, top=None, **kwargs):
            kwargs["on_pagination"](PaginationOutcome(len(self.ids), 1, False))
            return self.ids

        def query_work_items_batch(self, ids, fields):
            return [{
                "id": item_id,
                "fields": {
                    "System.Id": item_id,
                    "System.WorkItemType": "Feature",
                    "System.Title": "Ramp checkpoint",
                    "System.State": "Active",
                    "System.AreaPath": "One\\Adventure\\Acme",
                    "System.IterationPath": "One\\FY26\\Q4",
                    "System.ChangedDate": self.changed_at,
                },
            } for item_id in ids]

        def list_work_item_revisions(self, item_id, **kwargs):
            self.revision_calls.append(item_id)
            rows = [
                {"rev": 1, "fields": {"System.ChangedDate": "2026-05-08T08:00:00Z", "System.State": "Proposed"}},
                {"rev": 2, "fields": {"System.ChangedDate": self.changed_at, "System.State": "Active"}},
            ]
            kwargs["on_pagination"](PaginationOutcome(len(rows), 1, False))
            return rows

        def list_work_item_comments(self, item_id, **kwargs):
            from src.core.exceptions import QueryError

            self.comment_calls.append(item_id)
            if item_id in self.failed_comments:
                raise QueryError("synthetic comment failure")
            kwargs["on_pagination"](PaginationOutcome(0, 1, False))
            return []

    client = Client()
    binding = SimpleNamespace(
        config=ChannelConfig(channel="ado", enabled=True, discovery_threshold_hours=24, ttl_days=30),
        discovery_provider=ADODiscoveryProvider(client),
        discovery_config=ADODiscoveryConfig(load_slice_contract(contract_path)),
        hydration_provider=ADOHydrationProvider(client),
        hydration_config=ADOHydrationConfig(),
    )
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path))
    monkeypatch.setattr(gather, "_load_program_context", lambda *_args, **_kwargs: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda *_args, **_kwargs: (14, 30))
    monkeypatch.setattr(
        gather, "_resolve_uil_channel_binding_for_gather",
        lambda _program, _workstreams, channel, **_kwargs: binding if channel == "ado" else None,
    )
    monkeypatch.setattr(gather, "_gather_v2_enabled", lambda: gather_v2)
    monkeypatch.setattr(channel_runtime, "load_arch_data_fix", lambda *_args, **_kwargs: None)
    return programs_root, client


@pytest.mark.parametrize("gather_v2", [False, True])
@pytest.mark.parametrize("prior_commit", [False, True])
@pytest.mark.parametrize("failure_point", ["projection", "before_promotion", "after_promotion"])
def test_real_uil_ado_verification_waits_for_manifest_commit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, gather_v2: bool, prior_commit: bool, failure_point: str,
) -> None:
    from src.core import gather_run_manifest
    from src.core.channel_registry_store import ChannelRegistryStore
    from src.core.gather_run_manifest import get_verified_committed_run_state
    from src.core.program_paths import get_channel_registry_path

    programs_root, client = _real_uil_fixture(monkeypatch, tmp_path, gather_v2)
    initial_time = datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc)
    attempt_time = initial_time + timedelta(hours=2)
    if prior_commit:
        gather.gather_program("acme", as_of=initial_time, programs_root=programs_root, force_discovery=True)
    prior_ids, _ = get_verified_committed_run_state("acme", programs_root=programs_root)
    client.changed_at = "2026-05-10T09:00:00Z"
    before_attempt_calls = len(client.revision_calls)

    def interrupt(*_args, **_kwargs):
        raise RuntimeError("synthetic gather interruption")

    with monkeypatch.context() as interrupted:
        if failure_point == "projection":
            interrupted.setattr(gather, "run_projection_stage", interrupt)
        elif failure_point == "before_promotion":
            interrupted.setattr(gather, "commit_staging_run", interrupt)
        else:
            interrupted.setattr(gather_run_manifest, "_write_pointer", interrupt)
        with pytest.raises(RuntimeError, match="synthetic gather interruption"):
            gather.gather_program("acme", as_of=attempt_time, programs_root=programs_root, force_discovery=True)

    store = ChannelRegistryStore(get_channel_registry_path("acme", programs_root=programs_root), "acme")
    registration, = store.all_registrations("ado", provider_instance_id="default")
    assert registration.last_verified_at == (initial_time if prior_commit else None)
    committed_ids, _ = get_verified_committed_run_state("acme", programs_root=programs_root)
    assert len(committed_ids - prior_ids) == (1 if failure_point == "after_promotion" else 0)
    assert len(client.revision_calls) == before_attempt_calls + 1
    assert len(client.comment_calls) == before_attempt_calls + 1

    gather.gather_program("acme", as_of=attempt_time, programs_root=programs_root, force_discovery=True)
    registration, = store.all_registrations("ado", provider_instance_id="default")
    assert registration.last_verified_at == attempt_time
    assert len(client.revision_calls) == before_attempt_calls + 2
    assert len(client.comment_calls) == before_attempt_calls + 2


@pytest.mark.parametrize("gather_v2", [False, True])
@pytest.mark.parametrize("manifest_mode", ["shadow", "off"])
def test_real_uil_ado_verification_success_order_and_manifest_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, gather_v2: bool, manifest_mode: str,
) -> None:
    from src.core.channel_registry_store import ChannelRegistryStore
    from src.core.gather_run_manifest import get_verified_committed_run_state
    from src.core.workspace_lease import read_lease_state

    programs_root, _client = _real_uil_fixture(monkeypatch, tmp_path, gather_v2)
    monkeypatch.setattr(
        gather, "load_gather_runtime_policy",
        lambda *_args, **_kwargs: GatherRuntimePolicy(run_manifest_mode=manifest_mode),
    )
    original = ChannelRegistryStore.mark_verified
    observed_committed = []
    observed_lease = []

    def observe(self, *args, **kwargs):
        run_ids, _ = get_verified_committed_run_state("acme", programs_root=programs_root)
        observed_committed.append(bool(run_ids))
        observed_lease.append(
            read_lease_state("acme", mutation_domain=GATHER_MUTATION_DOMAIN, programs_root=programs_root) is not None
        )
        return original(self, *args, **kwargs)

    monkeypatch.setattr(ChannelRegistryStore, "mark_verified", observe)
    gather.gather_program(
        "acme", as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        programs_root=programs_root, force_discovery=True,
    )
    assert observed_committed == [manifest_mode == "shadow"]
    assert observed_lease == [manifest_mode == "shadow"]


@pytest.mark.parametrize("gather_v2", [False, True])
def test_real_uil_ado_verification_partial_commit_only_acknowledges_successes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, gather_v2: bool,
) -> None:
    from src.core.channel_registry_store import ChannelRegistryStore
    from src.core.program_paths import get_channel_registry_path

    programs_root, client = _real_uil_fixture(monkeypatch, tmp_path, gather_v2)
    client.ids = [1234, 5678]
    client.failed_comments = {5678}
    now = datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc)
    original = ChannelRegistryStore.mark_verified
    observed_statuses = []

    def observe(self, *args, **kwargs):
        manifest = resolve_latest_committed_manifest("acme", programs_root=programs_root)
        observed_statuses.append(manifest.required_scope_status if manifest is not None else None)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(ChannelRegistryStore, "mark_verified", observe)
    gather.gather_program("acme", as_of=now, programs_root=programs_root, force_discovery=True)

    manifest = resolve_latest_committed_manifest("acme", programs_root=programs_root)
    assert manifest is not None
    assert manifest.required_scope_status is RequiredScopeStatus.PARTIAL
    assert observed_statuses == [RequiredScopeStatus.PARTIAL]
    store = ChannelRegistryStore(get_channel_registry_path("acme", programs_root=programs_root), "acme")
    registrations = {row.ref_id: row for row in store.all_registrations("ado", provider_instance_id="default")}
    assert registrations["1234"].last_verified_at == now
    assert registrations["5678"].last_verified_at is None
    assert registrations["5678"].consecutive_hydration_failures == 1


@pytest.mark.parametrize("gather_v2", [False, True])
def test_real_uil_ado_verification_refuses_lost_post_commit_lease(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, gather_v2: bool,
) -> None:
    from src.core.channel_registry_store import ChannelRegistryStore
    from src.core.program_paths import get_channel_registry_path
    from src.core.workspace_lease import LeaseRenewalHeartbeat, read_lease_state

    programs_root, _client = _real_uil_fixture(monkeypatch, tmp_path, gather_v2)
    now = datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc)
    original = LeaseRenewalHeartbeat.renew_now
    renewal_calls = 0

    def renew(self):
        nonlocal renewal_calls
        renewal_calls += 1
        if renewal_calls == 2:
            raise RuntimeError("synthetic post-commit lease loss")
        return original(self)

    monkeypatch.setattr(LeaseRenewalHeartbeat, "renew_now", renew)
    with pytest.raises(RuntimeError, match="synthetic post-commit lease loss"):
        gather.gather_program("acme", as_of=now, programs_root=programs_root, force_discovery=True)
    assert resolve_latest_committed_manifest("acme", programs_root=programs_root) is not None
    store = ChannelRegistryStore(get_channel_registry_path("acme", programs_root=programs_root), "acme")
    registration, = store.all_registrations("ado", provider_instance_id="default")
    assert registration.last_verified_at is None
    assert read_lease_state("acme", mutation_domain=GATHER_MUTATION_DOMAIN, programs_root=programs_root) is None


@pytest.mark.parametrize("gather_v2", [False, True])
def test_real_uil_ado_verification_failure_after_commit_is_visible(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, gather_v2: bool,
) -> None:
    from src.core.channel_registry_store import ChannelRegistryStore
    from src.core.program_paths import get_channel_registry_path

    programs_root, client = _real_uil_fixture(monkeypatch, tmp_path, gather_v2)
    now = datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc)
    with monkeypatch.context() as interrupted:
        def fail_verification(*_args, **_kwargs):
            raise RuntimeError("synthetic acknowledgment failure")

        interrupted.setattr(ChannelRegistryStore, "mark_verified", fail_verification)
        with pytest.raises(RuntimeError, match="synthetic acknowledgment failure"):
            gather.gather_program("acme", as_of=now, programs_root=programs_root, force_discovery=True)

    assert resolve_latest_committed_manifest("acme", programs_root=programs_root) is not None
    store = ChannelRegistryStore(get_channel_registry_path("acme", programs_root=programs_root), "acme")
    registration, = store.all_registrations("ado", provider_instance_id="default")
    assert registration.last_verified_at is None
    gather.gather_program("acme", as_of=now, programs_root=programs_root, force_discovery=True)
    registration, = store.all_registrations("ado", provider_instance_id="default")
    assert registration.last_verified_at == now
    assert client.revision_calls == [1234, 1234]


def test_gather_program_stamps_signals_with_committed_run_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from src.core.journal import read_signals

    programs_root = tmp_path / "programs"
    program, workstreams = _fixture_program()
    item = _fixture_item_with_state_change()
    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))

    as_of = datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc)
    gather.gather_program(
        "acme",
        as_of=as_of,
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of, **_: ((item,), 3),
    )

    committed = resolve_latest_committed_manifest("acme", programs_root=programs_root)
    assert committed is not None

    signals = read_signals("acme", programs_root=programs_root)
    assert signals, "expected at least one signal from the state-change revision"
    assert all(signal.gather_run_id == committed.run_id for signal in signals)


@pytest.mark.parametrize("storage_backend", ["file", "sqlite"])
@pytest.mark.parametrize("gather_v2", [False, True])
@pytest.mark.parametrize("source_kind", ["workiq", "fallback"])
def test_gather_preserves_committed_signal_when_repeated_run_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    storage_backend: str,
    gather_v2: bool,
    source_kind: str,
) -> None:
    from dataclasses import replace

    from src.core.models import Confidence
    from src.core.models_v2 import Signal, TeamsChat, WorkstreamSignalSources

    programs_root = tmp_path / "programs"
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path))
    program, workstreams = _fixture_program()
    program = replace(program, storage_backend=storage_backend)
    workstreams = (
        replace(
            workstreams[0],
            signal_sources=WorkstreamSignalSources(
                teams_chats=(TeamsChat(display_name="Chat", work_item_ids=(1234,)),),
            ),
        ),
    )
    as_of = datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc)
    source = "workiq/teams" if source_kind == "workiq" else "synthetic/source"
    signal = Signal(
        id="transient-signal",
        timestamp=as_of,
        source=source,
        program_id=program.id,
        workstream_id="acme",
        entity_refs=("WS:acme",) if source_kind == "workiq" else ("WI:1234",),
        text="Synthetic update",
        raw_ref="synthetic-ref",
        confidence=Confidence.MEDIUM,
        metadata={"message_id": "native-message-1"} if source_kind == "workiq" else {"event_id": "event-1"},
    )
    monkeypatch.setattr(gather, "_load_program_context", lambda *_args, **_kwargs: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda *_args, **_kwargs: (14, 30))
    monkeypatch.setattr(gather, "_gather_v2_enabled", lambda: gather_v2)
    if source_kind == "workiq":
        monkeypatch.setattr(gather, "_resolve_workiq_signals", lambda **_kwargs: (signal,))

    gather_args = {
        "as_of": as_of,
        "programs_root": programs_root,
        "loader": lambda *_args, **_kwargs: ((), 0),
        "include_workiq": source_kind == "workiq",
        "include_analytics": source_kind == "fallback",
        "analytics_signal_loader": lambda *_args: ((signal,), 0),
    }
    gather.gather_program("acme", **gather_args)
    committed = resolve_latest_committed_manifest("acme", programs_root=programs_root)
    assert committed is not None
    store = gather.build_program_signal_store(program, programs_root=programs_root)
    before_retry = tuple(entry for entry in store.read(program.id) if entry.source == source)
    assert len(before_retry) == 1
    assert before_retry[0].gather_run_id == committed.run_id

    with monkeypatch.context() as interrupted:
        interrupted.setattr(
            gather,
            "run_projection_stage",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("synthetic gather interruption")),
        )
        with pytest.raises(RuntimeError, match="synthetic gather interruption"):
            gather.gather_program("acme", **gather_args)

    assert tuple(entry for entry in store.read(program.id) if entry.source == source) == before_retry
    assert resolve_latest_committed_manifest("acme", programs_root=programs_root) == committed


@pytest.mark.parametrize("gather_v2", [False, True])
def test_gather_out_of_window_sqlite_collision_preserves_committed_signal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, gather_v2: bool,
) -> None:
    from dataclasses import replace

    from src.core.models import Confidence
    from src.core.models_v2 import Signal

    programs_root = tmp_path / "programs"
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path))
    program, workstreams = _fixture_program()
    program = replace(program, storage_backend="sqlite")
    as_of = datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc)
    signal = Signal(
        id="transient-signal",
        timestamp=as_of,
        source="synthetic/source",
        program_id=program.id,
        workstream_id="acme",
        entity_refs=("WI:1234",),
        text="Synthetic update",
        raw_ref="synthetic-ref",
        confidence=Confidence.MEDIUM,
        metadata={"event_id": "event-1"},
    )
    monkeypatch.setattr(gather, "_load_program_context", lambda *_args, **_kwargs: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda *_args, **_kwargs: (14, 30))
    monkeypatch.setattr(gather, "_gather_v2_enabled", lambda: gather_v2)
    gather_args = {
        "programs_root": programs_root,
        "loader": lambda *_args, **_kwargs: ((), 0),
        "include_analytics": True,
        "analytics_signal_loader": lambda *_args: ((signal,), 0),
    }

    gather.gather_program("acme", as_of=as_of, **gather_args)
    committed = resolve_latest_committed_manifest("acme", programs_root=programs_root)
    assert committed is not None
    store = gather.build_program_signal_store(program, programs_root=programs_root)
    before = tuple(entry for entry in store.read(program.id) if entry.source == signal.source)
    assert len(before) == 1
    assert before[0].gather_run_id == committed.run_id
    later = as_of + timedelta(days=60)
    assert store.read(program.id, start=later - timedelta(days=14), end=later) == ()

    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        gather.gather_program("acme", as_of=later, **gather_args)

    assert tuple(entry for entry in store.read(program.id) if entry.source == signal.source) == before
    assert resolve_latest_committed_manifest("acme", programs_root=programs_root) == committed
    failed_root = programs_root / program.id / "runtime" / "gather_runs" / "failed"
    assert any(read_manifest(path).status is GatherRunStatus.FAILED for path in failed_root.iterdir())


@pytest.mark.parametrize("storage_backend", ["file", "sqlite"])
@pytest.mark.parametrize("gather_v2", [False, True])
@pytest.mark.parametrize("failure_point", ["projection", "promotion"])
def test_gather_retry_commits_signal_from_failed_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    storage_backend: str,
    gather_v2: bool,
    failure_point: str,
) -> None:
    from dataclasses import replace

    programs_root = tmp_path / "programs"
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path))
    program, workstreams = _fixture_program()
    program = replace(program, storage_backend=storage_backend)
    item = _fixture_item_with_state_change()
    monkeypatch.setattr(gather, "_load_program_context", lambda *_args, **_kwargs: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda *_args, **_kwargs: (14, 30))
    monkeypatch.setattr(gather, "_gather_v2_enabled", lambda: gather_v2)

    def fail_after_persistence(*_args, **_kwargs):
        raise RuntimeError("synthetic gather interruption")

    gather_args = {
        "as_of": item.fetched_at,
        "programs_root": programs_root,
        "loader": lambda *_args, **_kwargs: ((item,), 3),
    }
    with monkeypatch.context() as interrupted:
        interrupted.setattr(
            gather,
            "run_projection_stage" if failure_point == "projection" else "commit_staging_run",
            fail_after_persistence,
        )
        with pytest.raises(RuntimeError, match="synthetic gather interruption"):
            gather.gather_program("acme", **gather_args)

    signal_store = gather.build_program_signal_store(program, programs_root=programs_root)
    failed_signals = tuple(signal for signal in signal_store.read("acme") if signal.source == "ado/revision")
    assert len(failed_signals) == 1
    assert failed_signals[0].gather_run_id is not None
    assert resolve_latest_committed_manifest("acme", programs_root=programs_root) is None

    retry = gather.gather_program("acme", **gather_args)
    committed = resolve_latest_committed_manifest("acme", programs_root=programs_root)
    assert committed is not None
    assert committed.required_scope_status is RequiredScopeStatus.PARTIAL
    assert retry.new_signals >= 1
    assert any(
        signal.source == "ado/revision" and signal.gather_run_id == committed.run_id
        for signal in signal_store.read("acme")
    )

    repeated = gather.gather_program("acme", **gather_args)
    assert repeated.new_signals == 0


@pytest.mark.parametrize("storage_backend", ["file", "sqlite"])
def test_gather_retry_commits_signal_derived_facts_from_failed_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, storage_backend: str,
) -> None:
    from dataclasses import replace

    from src.core.models import Comment
    from src.core.program_fact_store import load_program_facts

    programs_root = tmp_path / "programs"
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path))
    program, workstreams = _fixture_program()
    program = replace(program, storage_backend=storage_backend)
    item = _fixture_item_with_state_change()
    item.comments = [
        Comment(1234, 17, "maintainer", "maintainer@example.com", item.fetched_at, "Action: follow up with alice."),
        Comment(1234, 18, "maintainer", "maintainer@example.com", item.fetched_at, "Decision: agreed to proceed."),
    ]
    monkeypatch.setattr(gather, "_load_program_context", lambda *_args, **_kwargs: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda *_args, **_kwargs: (14, 30))

    def fail_after_persistence(*_args, **_kwargs):
        raise RuntimeError("synthetic gather interruption")

    gather_args = {
        "as_of": item.fetched_at,
        "programs_root": programs_root,
        "loader": lambda *_args, **_kwargs: ((item,), 3),
    }
    with monkeypatch.context() as interrupted:
        interrupted.setattr(gather, "run_projection_stage", fail_after_persistence)
        with pytest.raises(RuntimeError, match="synthetic gather interruption"):
            gather.gather_program("acme", **gather_args)

    failed_facts = load_program_facts("acme", as_of=datetime.now(timezone.utc), programs_root=programs_root)
    assert {"action.item", "decision.entry"} <= {fact.fact_type for fact in failed_facts.facts}

    gather.gather_program("acme", **gather_args)
    committed = resolve_latest_committed_manifest("acme", programs_root=programs_root)
    assert committed is not None
    committed_facts = load_program_facts(
        "acme", as_of=datetime.now(timezone.utc), programs_root=programs_root,
        require_committed_gather_run=True,
    )
    assert {"action.item", "decision.entry"} <= {
        fact.fact_type for fact in committed_facts.facts if fact.gather_run_id == committed.run_id
    }


def test_gather_program_fails_manifest_on_exception(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program, workstreams = _fixture_program()
    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))

    def _boom(*_a, **_k):
        raise RuntimeError("loader exploded")

    with pytest.raises(RuntimeError, match="loader exploded"):
        gather.gather_program(
            "acme",
            as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
            programs_root=programs_root,
            loader=_boom,
        )

    assert resolve_latest_committed_manifest("acme", programs_root=programs_root) is None

    failed_root = programs_root / "acme" / "runtime" / "gather_runs" / "failed"
    failed_runs = list(failed_root.iterdir())
    assert len(failed_runs) == 1
    failed_manifest = read_manifest(failed_runs[0])
    assert failed_manifest.status is GatherRunStatus.FAILED

    # Lease released even on failure -- a subsequent gather can proceed.
    handle = acquire_lease("acme", "next-caller", mutation_domain=GATHER_MUTATION_DOMAIN, programs_root=programs_root)
    assert handle.owner == "next-caller"


def test_required_ado_degradation_commits_partial_without_advancing_full_pointer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    programs_root = tmp_path / "programs"
    artifacts = GatherArtifacts(
        program_id="acme", scanned_items=0, discovered_signals=0, new_signals=0,
        pending_review=0, trajectory_updates=0, auto_reviews_written=0, ado_calls=1,
        integration_errors=(IntegrationError(source="ado", stage="future-stage", message="failed", retryable=False),),
    )
    monkeypatch.setattr(gather, "_gather_program_impl", lambda *_args, **_kwargs: artifacts)

    gather.gather_program(
        "acme", as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc), programs_root=programs_root
    )

    committed = resolve_latest_committed_manifest("acme", programs_root=programs_root)
    assert committed is not None
    assert committed.required_scope_status is RequiredScopeStatus.PARTIAL
    assert resolve_latest_full_committed_manifest("acme", programs_root=programs_root) is None


def test_gather_program_persists_captured_query_membership_sidecars(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    programs_root = tmp_path / "programs"
    captured_at = datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc)
    artifacts = GatherArtifacts(
        program_id="acme", scanned_items=99, discovered_signals=0, new_signals=0,
        pending_review=0, trajectory_updates=0, auto_reviews_written=0, ado_calls=2,
        ado_query_results=(
            DiscoveryQueryResult(
                query_id="query-1", scope_id="slice:binding", wiql_hash="a" * 64,
                captured_at=captured_at, raw_count=2, membership_ids=("101", "202"),
                membership_hash="b" * 64, cap_reached=False, completeness_state="FULL",
            ),
        ),
        discovered_work_item_ids=("101", "202", "303"),
        hydrated_work_item_ids=("101", "202"),
    )
    monkeypatch.setattr(gather, "_gather_program_impl", lambda *_args, **_kwargs: artifacts)

    gather.gather_program("acme", as_of=captured_at, programs_root=programs_root)

    committed = resolve_latest_committed_manifest("acme", programs_root=programs_root)
    assert committed is not None
    assert committed.discovered_count == 3
    assert committed.hydrated_count == 2
    assert committed.query_results[0].membership_ids == ("101", "202")
    assert committed.required_scope_status is RequiredScopeStatus.FULL
    assert committed.last_successful_full_discovery_at == captured_at
    assert committed.next_expected_run_at == captured_at + timedelta(hours=24)
    assert committed.consecutive_failed_runs == 0
    assert committed.freshness_state == "current"
    assert committed.ado_items_hash is not None
    assert committed.query_results_hash is not None
    run_dir = get_committed_run_dir("acme", committed.run_id, programs_root=programs_root)
    assert (run_dir / "ado_items.jsonl").exists()
    assert (run_dir / "query_results.json").exists()


def test_gather_program_records_operator_source_export_reconciliation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """D-19/AG-2.12: ``source_export_counts`` reconciles each scope's
    committed ``QueryResultEntry.oracle_result`` beyond the default weak
    same-endpoint-rerun proof -- matching scopes report a match, disagreeing
    scopes report a mismatch, and scopes with no operator export recorded
    keep the explicit same-endpoint-rerun default."""
    programs_root = tmp_path / "programs"
    captured_at = datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc)
    artifacts = GatherArtifacts(
        program_id="acme", scanned_items=0, discovered_signals=0, new_signals=0,
        pending_review=0, trajectory_updates=0, auto_reviews_written=0, ado_calls=2,
        ado_query_results=(
            DiscoveryQueryResult(
                query_id="query-1", scope_id="scope-match", wiql_hash="a" * 64,
                captured_at=captured_at, raw_count=10, membership_ids=(),
                membership_hash="b" * 64, cap_reached=False, completeness_state="FULL",
            ),
            DiscoveryQueryResult(
                query_id="query-2", scope_id="scope-mismatch", wiql_hash="c" * 64,
                captured_at=captured_at, raw_count=10, membership_ids=(),
                membership_hash="d" * 64, cap_reached=False, completeness_state="FULL",
            ),
            DiscoveryQueryResult(
                query_id="query-3", scope_id="scope-unrecorded", wiql_hash="e" * 64,
                captured_at=captured_at, raw_count=10, membership_ids=(),
                membership_hash="f" * 64, cap_reached=False, completeness_state="FULL",
            ),
        ),
    )
    monkeypatch.setattr(gather, "_gather_program_impl", lambda *_args, **_kwargs: artifacts)

    gather.gather_program(
        "acme",
        as_of=captured_at,
        programs_root=programs_root,
        source_export_counts={"scope-match": 10, "scope-mismatch": 7},
    )

    committed = resolve_latest_committed_manifest("acme", programs_root=programs_root)
    assert committed is not None
    results_by_scope = {result.scope_id: result for result in committed.query_results}
    assert results_by_scope["scope-match"].oracle_result == ORACLE_RESULT_OPERATOR_EXPORT_MATCH
    assert results_by_scope["scope-mismatch"].oracle_result == "operator_source_export:mismatch:reported=7:observed=10"
    assert results_by_scope["scope-unrecorded"].oracle_result == ORACLE_RESULT_SAME_ENDPOINT_RERUN


def test_gather_program_uses_program_configured_manifest_timing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    programs_root = tmp_path / "programs"
    captured_at = datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc)
    artifacts = GatherArtifacts(
        program_id="acme",
        scanned_items=1,
        discovered_signals=0,
        new_signals=0,
        pending_review=0,
        trajectory_updates=0,
        auto_reviews_written=0,
        ado_calls=1,
        ado_query_results=(
            DiscoveryQueryResult(
                query_id="query-1",
                scope_id="slice:binding",
                wiql_hash="a" * 64,
                captured_at=captured_at,
                raw_count=1,
                membership_ids=("101",),
                membership_hash="b" * 64,
                cap_reached=False,
                completeness_state="FULL",
            ),
        ),
        discovered_work_item_ids=("101",),
        hydrated_work_item_ids=("101",),
    )
    monkeypatch.setattr(gather, "_gather_program_impl", lambda *_args, **_kwargs: artifacts)
    monkeypatch.setattr(
        gather,
        "load_gather_runtime_policy",
        lambda *_args, **_kwargs: GatherRuntimePolicy(
            run_manifest_mode="shadow",
            full_discovery_cadence_hours=6,
            freshness_warn_hours=7,
            freshness_block_hours=8,
        ),
    )

    gather.gather_program("acme", as_of=captured_at, programs_root=programs_root)

    committed = resolve_latest_full_committed_manifest("acme", programs_root=programs_root)
    assert committed is not None
    assert committed.next_expected_run_at == captured_at + timedelta(hours=6)
    assert gather.freshness_state(
        last_successful_full_discovery_at=captured_at,
        now=captured_at + timedelta(hours=7),
        warn_hours=7,
        block_hours=8,
    ) == "warn"
    assert gather.freshness_state(
        last_successful_full_discovery_at=captured_at,
        now=captured_at + timedelta(hours=8),
        warn_hours=7,
        block_hours=8,
    ) == "block"


def test_gather_program_off_mode_preserves_legacy_artifact_behavior(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    programs_root = tmp_path / "programs"
    expected = GatherArtifacts(
        program_id="acme",
        scanned_items=1,
        discovered_signals=0,
        new_signals=0,
        pending_review=0,
        trajectory_updates=0,
        auto_reviews_written=0,
        ado_calls=0,
    )
    observed_run_ids: list[str | None] = []

    def _legacy_gather(*_args, **kwargs) -> GatherArtifacts:
        observed_run_ids.append(kwargs["gather_run_id"])
        return expected

    monkeypatch.setattr(gather, "_gather_program_impl", _legacy_gather)
    monkeypatch.setattr(
        gather,
        "load_gather_runtime_policy",
        lambda *_args, **_kwargs: GatherRuntimePolicy(run_manifest_mode="off"),
    )

    assert gather.gather_program("acme", programs_root=programs_root) is expected
    assert observed_run_ids == [None]
    assert not (programs_root / "acme" / "runtime" / "gather_runs").exists()


def test_gather_program_records_alerts_observed_during_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    programs_root = tmp_path / "programs"
    captured_at = datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc)
    artifacts = GatherArtifacts(
        program_id="acme", scanned_items=0, discovered_signals=0, new_signals=0,
        pending_review=0, trajectory_updates=0, auto_reviews_written=0, ado_calls=0,
    )

    def _emit_alert(*_args, **_kwargs):
        append_or_suppress_alert(
            program_id="acme",
            category="channel_budget_exceeded",
            entity_type="channel",
            entity_id="kusto",
            severity="warn",
            message="Kusto budget exceeded.",
            next_command="vertex cockpit show --program acme",
            programs_root=programs_root,
        )
        return artifacts

    monkeypatch.setattr(gather, "_gather_program_impl", _emit_alert)

    gather.gather_program("acme", as_of=captured_at, programs_root=programs_root)

    committed = resolve_latest_committed_manifest("acme", programs_root=programs_root)
    assert committed is not None
    assert len(committed.alert_ids) == 1
    assert committed.alert_delivery_failed is False


def test_gather_program_marks_excessive_query_capture_skew_partial(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    programs_root = tmp_path / "programs"
    first_capture = datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc)
    artifacts = GatherArtifacts(
        program_id="acme", scanned_items=0, discovered_signals=0, new_signals=0,
        pending_review=0, trajectory_updates=0, auto_reviews_written=0, ado_calls=2,
        ado_query_results=(
            DiscoveryQueryResult(
                query_id="query-1", scope_id="slice:one", wiql_hash="a" * 64,
                captured_at=first_capture, raw_count=0, membership_ids=(),
                membership_hash="b" * 64, cap_reached=False, completeness_state="FULL",
            ),
            DiscoveryQueryResult(
                query_id="query-2", scope_id="slice:two", wiql_hash="c" * 64,
                captured_at=first_capture.replace(minute=6), raw_count=0, membership_ids=(),
                membership_hash="d" * 64, cap_reached=False, completeness_state="FULL",
            ),
        ),
    )
    monkeypatch.setattr(gather, "_gather_program_impl", lambda *_args, **_kwargs: artifacts)

    gather.gather_program("acme", as_of=first_capture, programs_root=programs_root)

    committed = resolve_latest_committed_manifest("acme", programs_root=programs_root)
    assert committed is not None
    assert committed.first_query_captured_at == first_capture
    assert committed.last_query_captured_at == first_capture.replace(minute=6)
    assert committed.query_capture_skew_seconds == 360
    assert committed.required_scope_status is RequiredScopeStatus.PARTIAL
    assert resolve_latest_full_committed_manifest("acme", programs_root=programs_root) is None


def test_gather_program_releases_lease_when_manifest_commit_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A promotion I/O failure must not block the next operator until lease TTL."""
    programs_root = tmp_path / "programs"
    program, workstreams = _fixture_program()
    item = _fixture_item()
    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))
    monkeypatch.setattr(gather, "commit_staging_run", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk locked")))

    with pytest.raises(OSError, match="disk locked"):
        gather.gather_program(
            "acme",
            as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
            programs_root=programs_root,
            loader=lambda *_args, **_kwargs: ((item,), 3),
        )

    handle = acquire_lease("acme", "next-caller", mutation_domain=GATHER_MUTATION_DOMAIN, programs_root=programs_root)
    assert handle.owner == "next-caller"


@pytest.mark.parametrize(
    "failure_point",
    [
        "quarantine", "alert_before", "previous_run", "staging", "staging_created",
        "heartbeat_construct", "heartbeat_start", "heartbeat_started",
        "alert_after", "ado_sidecar", "query_sidecar", "hash",
    ],
)
@pytest.mark.parametrize("prior_commit", [False, True])
def test_gather_program_releases_lease_on_setup_or_finalization_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure_point: str, prior_commit: bool,
) -> None:
    from src.core import workspace_lease

    programs_root = tmp_path / "programs"
    artifacts = GatherArtifacts(
        program_id="acme", scanned_items=0, discovered_signals=0, new_signals=0,
        pending_review=0, trajectory_updates=0, auto_reviews_written=0, ado_calls=0,
    )
    monkeypatch.setattr(gather, "_gather_program_impl", lambda *_args, **_kwargs: artifacts)
    if prior_commit:
        gather.gather_program("acme", programs_root=programs_root)
    previous = resolve_latest_committed_manifest("acme", programs_root=programs_root)

    heartbeats = []
    original_heartbeat = workspace_lease.LeaseRenewalHeartbeat

    class TrackingHeartbeat(original_heartbeat):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            heartbeats.append(self)

        def start(self):
            super().start()
            if failure_point == "heartbeat_started":
                raise OSError("synthetic setup failure")

    def interrupt(*_args, **_kwargs):
        raise OSError("synthetic setup or finalization failure")

    with monkeypatch.context() as interrupted:
        interrupted.setattr(gather, "LeaseRenewalHeartbeat", TrackingHeartbeat)
        if failure_point == "heartbeat_construct":
            interrupted.setattr(gather, "LeaseRenewalHeartbeat", interrupt)
        elif failure_point == "heartbeat_start":
            interrupted.setattr(workspace_lease.Thread, "start", interrupt)
        elif failure_point == "quarantine":
            interrupted.setattr(gather, "quarantine_abandoned_staging_runs", interrupt)
        elif failure_point == "alert_before":
            interrupted.setattr(gather, "_alert_ledger_state", interrupt)
        elif failure_point == "alert_after":
            original_alert_state = gather._alert_ledger_state
            alert_reads = 0

            def alert_state_then_interrupt(*args, **kwargs):
                nonlocal alert_reads
                alert_reads += 1
                if alert_reads == 2:
                    interrupt()
                return original_alert_state(*args, **kwargs)

            interrupted.setattr(gather, "_alert_ledger_state", alert_state_then_interrupt)
        elif failure_point == "previous_run":
            interrupted.setattr(gather, "resolve_latest_committed_manifest", interrupt)
        elif failure_point == "staging":
            interrupted.setattr(gather, "create_staging_manifest", interrupt)
        elif failure_point == "staging_created":
            original_create = gather.create_staging_manifest

            def create_then_interrupt(*args, **kwargs):
                original_create(*args, **kwargs)
                interrupt()

            interrupted.setattr(gather, "create_staging_manifest", create_then_interrupt)
        elif failure_point == "ado_sidecar":
            interrupted.setattr(gather, "write_ado_items", interrupt)
        elif failure_point == "query_sidecar":
            interrupted.setattr(gather, "write_query_results_sidecar", interrupt)
        elif failure_point == "hash":
            interrupted.setattr(gather, "hash_ado_items", interrupt)
        with pytest.raises(OSError, match="synthetic"):
            gather.gather_program("acme", programs_root=programs_root)

    assert workspace_lease.read_lease_state(
        "acme", mutation_domain=GATHER_MUTATION_DOMAIN, programs_root=programs_root,
    ) is None
    for heartbeat in heartbeats:
        assert heartbeat._thread is None or not heartbeat._thread.is_alive()
    assert resolve_latest_committed_manifest("acme", programs_root=programs_root) == previous
    assert acquire_lease(
        "acme", "next-caller", mutation_domain=GATHER_MUTATION_DOMAIN, programs_root=programs_root,
    ).owner == "next-caller"


def test_gather_program_releases_lease_when_heartbeat_stop_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from src.core import workspace_lease

    programs_root = tmp_path / "programs"
    artifacts = GatherArtifacts(
        program_id="acme", scanned_items=0, discovered_signals=0, new_signals=0,
        pending_review=0, trajectory_updates=0, auto_reviews_written=0, ado_calls=0,
    )
    monkeypatch.setattr(gather, "_gather_program_impl", lambda *_args, **_kwargs: artifacts)

    class FailingStopHeartbeat(workspace_lease.LeaseRenewalHeartbeat):
        def stop(self):
            super().stop()
            raise OSError("synthetic heartbeat-stop failure")

    monkeypatch.setattr(gather, "LeaseRenewalHeartbeat", FailingStopHeartbeat)
    with pytest.raises(OSError, match="synthetic heartbeat-stop failure"):
        gather.gather_program("acme", programs_root=programs_root)

    assert resolve_latest_committed_manifest("acme", programs_root=programs_root) is not None
    assert workspace_lease.read_lease_state(
        "acme", mutation_domain=GATHER_MUTATION_DOMAIN, programs_root=programs_root,
    ) is None


@pytest.mark.parametrize("failure_point", ["staging_created", "ado_sidecar", "query_sidecar", "hash"])
def test_gather_program_quarantines_interrupted_finalization_on_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure_point: str,
) -> None:
    programs_root = tmp_path / "programs"
    artifacts = GatherArtifacts(
        program_id="acme", scanned_items=0, discovered_signals=0, new_signals=0,
        pending_review=0, trajectory_updates=0, auto_reviews_written=0, ado_calls=0,
    )
    monkeypatch.setattr(gather, "_gather_program_impl", lambda *_args, **_kwargs: artifacts)
    before = gather.gather_program("acme", programs_root=programs_root)
    committed = resolve_latest_committed_manifest("acme", programs_root=programs_root)
    assert before == artifacts
    assert committed is not None

    with monkeypatch.context() as interrupted:
        def interrupt(*_args, **_kwargs):
            raise OSError("synthetic finalization failure")

        if failure_point == "staging_created":
            original_create = gather.create_staging_manifest

            def create_then_interrupt(*args, **kwargs):
                original_create(*args, **kwargs)
                interrupt()

            interrupted.setattr(gather, "create_staging_manifest", create_then_interrupt)
        else:
            target = {
                "ado_sidecar": "write_ado_items",
                "query_sidecar": "write_query_results_sidecar",
                "hash": "hash_ado_items",
            }[failure_point]
            interrupted.setattr(gather, target, interrupt)
        with pytest.raises(OSError, match="synthetic finalization failure"):
            gather.gather_program("acme", programs_root=programs_root)

    staging_root = programs_root / "acme" / "runtime" / "gather_runs" / "staging"
    staged, = tuple(staging_root.iterdir())
    staged_manifest = read_manifest(staged)
    assert staged_manifest.status is GatherRunStatus.RUNNING
    assert resolve_latest_committed_manifest("acme", programs_root=programs_root) == committed

    gather.gather_program("acme", programs_root=programs_root)
    quarantined = get_quarantine_run_dir("acme", staged_manifest.run_id, programs_root=programs_root)
    assert read_manifest(quarantined).status is GatherRunStatus.QUARANTINED
    assert not staged.exists()
    assert resolve_latest_committed_manifest("acme", programs_root=programs_root).run_id != committed.run_id


def test_gather_program_does_not_quarantine_before_acquiring_lease(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    programs_root = tmp_path / "programs"
    held = acquire_lease("acme", "other-host", mutation_domain=GATHER_MUTATION_DOMAIN, programs_root=programs_root)
    started = datetime(2026, 5, 1, 8, 0, tzinfo=timezone.utc)
    active = GatherRunManifest(
        run_id=f"gather-{new_ulid(started)}", status=GatherRunStatus.RUNNING,
        program_id="acme", actor_identity_type="interactive", lease_owner=held.owner,
        lease_fencing_token=held.fencing_token, started_at=started, scope_as_of=started,
        required_scope_status=RequiredScopeStatus.FULL,
    )
    abandoned = GatherRunManifest(
        run_id=f"gather-{new_ulid(started)}", status=GatherRunStatus.RUNNING,
        program_id="acme", actor_identity_type="interactive", lease_owner="crashed-worker",
        lease_fencing_token=held.fencing_token - 1, started_at=started, scope_as_of=started,
        required_scope_status=RequiredScopeStatus.FULL,
    )
    for manifest in (active, abandoned):
        create_staging_manifest(manifest, programs_root=programs_root)
    before = {
        manifest.run_id: (get_staging_run_dir("acme", manifest.run_id, programs_root=programs_root) / "manifest.json").read_bytes()
        for manifest in (active, abandoned)
    }

    with pytest.raises(gather.GatherLeaseConflict):
        gather.gather_program("acme", programs_root=programs_root)

    for manifest in (active, abandoned):
        staged = get_staging_run_dir("acme", manifest.run_id, programs_root=programs_root)
        assert (staged / "manifest.json").read_bytes() == before[manifest.run_id]
        assert not get_quarantine_run_dir("acme", manifest.run_id, programs_root=programs_root).exists()


def test_gather_program_releases_lease_when_failure_manifest_write_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    programs_root = tmp_path / "programs"
    program, workstreams = _fixture_program()
    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_gather_program_impl", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("loader failed")))
    monkeypatch.setattr(gather, "fail_staging_run", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("failure write locked")))

    with pytest.raises(OSError, match="failure write locked"):
        gather.gather_program("acme", as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc), programs_root=programs_root)

    assert acquire_lease("acme", "next-caller", mutation_domain=GATHER_MUTATION_DOMAIN, programs_root=programs_root).owner == "next-caller"


def test_gather_program_lease_conflict_fails_fast(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program, workstreams = _fixture_program()
    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    acquire_lease("acme", "other-host", mutation_domain=GATHER_MUTATION_DOMAIN, ttl_seconds=300, programs_root=programs_root)

    called = False

    def _loader(*_a, **_k):
        nonlocal called
        called = True
        return ((), 0)

    with pytest.raises(typer.BadParameter, match="already has a gather running"):
        gather.gather_program(
            "acme",
            as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
            programs_root=programs_root,
            loader=_loader,
        )

    assert called is False
    assert resolve_latest_committed_manifest("acme", programs_root=programs_root) is None


def test_gather_program_fencing_loss_fails_instead_of_promoting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The final synchronous renewal is a promotion fence, not best effort."""
    programs_root = tmp_path / "programs"
    artifacts = GatherArtifacts(
        program_id="acme", scanned_items=0, discovered_signals=0, new_signals=0,
        pending_review=0, trajectory_updates=0, auto_reviews_written=0, ado_calls=0,
    )
    monkeypatch.setattr(gather, "_gather_program_impl", lambda *_args, **_kwargs: artifacts)

    class _StaleHeartbeat:
        def __init__(self, handle, **_kwargs) -> None:
            self.handle = handle

        def start(self) -> None:
            return None

        def stop(self) -> None:
            return None

        def renew_now(self):
            raise LeaseFencingTokenStale(self.handle.fencing_token, self.handle.fencing_token + 1)

    monkeypatch.setattr(gather, "LeaseRenewalHeartbeat", _StaleHeartbeat)

    with pytest.raises(LeaseFencingTokenStale):
        gather.gather_program(
            "acme",
            as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
            programs_root=programs_root,
        )

    assert resolve_latest_committed_manifest("acme", programs_root=programs_root) is None
    failed_runs = list((programs_root / "acme" / "runtime" / "gather_runs" / "failed").iterdir())
    assert len(failed_runs) == 1
    assert read_manifest(failed_runs[0]).status is GatherRunStatus.FAILED


def test_gather_program_quarantines_abandoned_staging_run_first(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program, workstreams = _fixture_program()
    item = _fixture_item()
    monkeypatch.setattr(gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))

    stale_started_at = datetime(2026, 5, 1, 8, 0, tzinfo=timezone.utc)
    abandoned = GatherRunManifest(
        run_id=f"gather-{new_ulid(stale_started_at)}",
        status=GatherRunStatus.RUNNING,
        program_id="acme",
        actor_identity_type="interactive",
        lease_owner="crashed-worker",
        lease_fencing_token=1,
        started_at=stale_started_at,
        scope_as_of=stale_started_at,
        required_scope_status=RequiredScopeStatus.FULL,
    )
    create_staging_manifest(abandoned, programs_root=programs_root)

    artifacts = gather.gather_program(
        "acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of, **_: ((item,), 3),
    )
    assert artifacts is not None

    quarantine_dir = get_quarantine_run_dir("acme", abandoned.run_id, programs_root=programs_root)
    assert quarantine_dir.exists()
    quarantined_manifest = read_manifest(quarantine_dir)
    assert quarantined_manifest.status is GatherRunStatus.QUARANTINED
    assert (quarantine_dir / "orphan_index.json").exists()

    committed = resolve_latest_committed_manifest("acme", programs_root=programs_root)
    assert committed is not None
    assert committed.run_id != abandoned.run_id
