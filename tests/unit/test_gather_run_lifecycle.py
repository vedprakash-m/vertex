"""D-13/Sec 4.6: tests for the gather-run manifest lifecycle wiring into
``gather_program`` (the lease-acquire / staging-manifest-create / delegate /
commit-or-fail / lease-release wrapper around the existing gather
implementation).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

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
def test_real_uil_discovery_outcome_reaches_committed_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, gather_v2: bool, query_fails: bool,
) -> None:
    from types import SimpleNamespace

    from src.commands.gather_pipeline import channel_runtime
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
        "    source_of_truth: ado_primary\n    owners:\n      primary: Owner\n"
        "    source_contract:\n      ado:\n        saved_queries: [good, failed]\n"
        "        tag_expression:\n          all_of: [Release]\n        required_fields: [state]\n"
        "    freshness:\n      warn_days: 5\n      block_days: 10\n"
        "    degradation:\n      blank_filter_is_error: true\n",
        encoding="utf-8",
    )

    class Client:
        def get_saved_query(self, query_id):
            if query_fails and query_id == "failed":
                raise QueryError("Synthetic saved query unavailable")
            return {"id": query_id, "wiql": "Select [System.Id] From WorkItems"}

        def execute_wiql(self, wiql, top=None, **kwargs):
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
    assert ado_outcomes[0].degraded is query_fails
    assert bool(ado_outcomes[0].degrade_reason) is query_fails
    assert committed.required_scope_status is (
        RequiredScopeStatus.PARTIAL if query_fails else RequiredScopeStatus.FULL
    )
    assert any(result.completeness_state == "ERROR" for result in committed.query_results) is query_fails
    assert (resolve_latest_full_committed_manifest("acme", programs_root=programs_root) is None) is query_fails


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
