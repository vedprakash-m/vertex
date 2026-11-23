from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.commands.gather_pipeline import channel_runtime
from src.core.models import RiskLevel, WorkItem


@pytest.mark.parametrize(
    "discovery_state",
    [
        "partial", "scope_partial", "scope_partial_no_reason", "scope_error", "returned_error",
        "exception", "empty", "incremental", "full_scope",
    ],
)
@pytest.mark.parametrize("hydration_error", [False, True])
def test_run_channel_preserves_discovery_degradation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, discovery_state: str, hydration_error: bool,
) -> None:
    from types import SimpleNamespace

    from src.core.channel_registry_store import ChannelRegistryStore
    from src.core.exceptions import QueryError
    from src.core.integration_types import (
        ADOHydrationOutput, ChannelConfig, DiscoveryCompleteness, DiscoveryResult,
        HydrationResult, IntegrationError, RunContext, ScopeStatus, ScopeStatusKind,
    )

    now = datetime(2026, 10, 28, tzinfo=timezone.utc)
    errors = []
    outcomes = []
    captures = []
    discovery_error = IntegrationError(
        source="ado", stage="discovery", message="Discovery unavailable", retryable=True,
        operator_action="Retry discovery",
    )
    scope_statuses = {}
    if discovery_state in {"scope_partial", "scope_partial_no_reason", "scope_error", "full_scope"}:
        scope_statuses["scope"] = ScopeStatus(
            scope_id="scope",
            status=ScopeStatusKind.ERROR if discovery_state == "scope_error" else ScopeStatusKind.SUCCESS,
            completeness=DiscoveryCompleteness.PARTIAL if discovery_state.startswith("scope_partial") else DiscoveryCompleteness.FULL,
            item_count=0,
            error_message=None if discovery_state == "scope_partial_no_reason" else "Scope discovery incomplete",
        )
    result = DiscoveryResult(
        channel="ado", program_id="demo", discovered_refs=(),
        completeness=(
            DiscoveryCompleteness.PARTIAL if discovery_state == "partial"
            else DiscoveryCompleteness.INCREMENTAL if discovery_state == "incremental"
            else DiscoveryCompleteness.FULL
        ),
        scope_statuses=scope_statuses, scope_state_updates={},
        errors=(discovery_error,) if discovery_state == "returned_error" else (),
        computed_at=now,
    )

    def discover(*args, **kwargs):
        if discovery_state == "exception":
            raise QueryError("Discovery unavailable")
        return result

    hydration = HydrationResult(
        channel="ado", resources=ADOHydrationOutput(work_items=(), freshness_items=()),
        api_call_count=2, hydrated_ref_ids=(), failed_ref_ids=(),
        errors=(
            IntegrationError(source="ado", stage="hydration", message="Hydration warning", retryable=False),
        ) if hydration_error else (),
    )
    binding = SimpleNamespace(
        config=ChannelConfig(channel="ado", enabled=True, discovery_threshold_hours=24, ttl_days=30),
        discovery_provider=SimpleNamespace(discover=discover),
        hydration_provider=SimpleNamespace(hydrate=lambda *args, **kwargs: hydration),
        discovery_config=object(), hydration_config=object(),
    )
    monkeypatch.setattr(channel_runtime, "load_arch_data_fix", lambda *args, **kwargs: None)
    hydrated, _ = channel_runtime.run_channel(
        binding, ChannelRegistryStore(tmp_path / "registry.sqlite3", "demo"),
        program_id="demo", since=now, verified_at=now, run_ctx=RunContext(force_discovery=True),
        integration_error_sink=errors, discovery_result_sink=captures, channel_outcome_sink=outcomes,
        programs_root=tmp_path,
    )

    assert hydrated == hydration
    assert len(outcomes) == 1
    degraded = hydration_error or discovery_state not in {"empty", "incremental", "full_scope"}
    assert outcomes[0].degraded is degraded
    assert outcomes[0].ado_call_count == 2
    if hydration_error:
        assert outcomes[0].degrade_reason == "Hydration warning"
    elif degraded:
        assert outcomes[0].degrade_reason
        if discovery_state == "exception":
            assert outcomes[0].degrade_reason == "ADO discovery runtime failed"
        elif discovery_state == "returned_error":
            assert outcomes[0].degrade_reason == "Discovery unavailable"
    else:
        assert outcomes[0].degrade_reason is None
    assert captures == ([] if discovery_state == "exception" else [result])
    if discovery_state == "returned_error":
        assert any(
            error.message == discovery_error.message and error.retryable
            and error.operator_action == "Retry discovery"
            for error in errors
        )


def test_run_channel_with_extraction_surfaces_extractor_errors(tmp_path: Path) -> None:
    from src.core.alerts import append_or_suppress_alert, read_alerts
    from src.core.channel_registry_store import ChannelRegistryStore
    from src.core.integration_types import (
        ADOHydrationOutput,
        ChannelBinding,
        ChannelConfig,
        ChannelRegistration,
        DiscoveredRef,
        DiscoveryCompleteness,
        DiscoveryQueryResult,
        DiscoveryResult,
        ExtractionResult,
        HydrationResult,
        IntegrationError,
        RegistrationBinding,
        RegistrationStatus,
        RunContext,
    )

    current_time = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    store = ChannelRegistryStore(tmp_path / "demo" / "channel_registry.sqlite3", "demo")
    item = WorkItem(
        id=101,
        type="Feature",
        title="Hydrated",
        state="Active",
        assigned_to="Owner",
        assigned_to_email="owner@example.com",
        area_path="One\\Demo",
        iteration_path="One\\Iteration",
        target_date=None,
        risk_level=RiskLevel.UNKNOWN,
        tags=["EXAMPLETAG1"],
        custom_fields={},
        fetched_at=current_time,
    )

    class _DiscoveryProvider:
        def discover(self, program_id, config, existing, run_ctx=None):
            del config, existing, run_ctx
            registration = ChannelRegistration(
                channel="ado",
                program_id=program_id,
                provider_instance_id="default",
                ref_id="101",
                ref_kind="work_item",
                status=RegistrationStatus.ACTIVE,
                first_discovered_at=current_time,
                last_seen_at=current_time,
                ref_title="[PII] demo title",
            )
            return DiscoveryResult(
                channel="ado",
                program_id=program_id,
                discovered_refs=(
                    DiscoveredRef(
                        registration=registration,
                        bindings=(
                            RegistrationBinding(
                                workstream_id="demo.slice",
                                scope_id="scope",
                                source_type="wiql_saved_query",
                                confidence=1.0,
                                confidence_source="wiql_saved_query",
                            ),
                        ),
                    ),
                ),
                completeness=DiscoveryCompleteness.FULL,
                scope_statuses={},
                scope_state_updates={},
                errors=(),
                computed_at=current_time,
                query_results=(
                    DiscoveryQueryResult(
                        query_id="query-1",
                        scope_id="scope",
                        wiql_hash="a" * 64,
                        captured_at=current_time,
                        raw_count=1,
                        membership_ids=("101",),
                        membership_hash="b" * 64,
                        cap_reached=False,
                        completeness_state="FULL",
                    ),
                ),
            )

    class _HydrationProvider:
        def hydrate(self, registrations, since, program_id, config, mode=None, run_ctx=None):
            del registrations, since, program_id, config, mode, run_ctx
            return HydrationResult(
                channel="ado",
                resources=ADOHydrationOutput(
                    work_items=(item,),
                    freshness_items=(item,),
                    checkpoint_reseed_reasons=((101, "anti_entropy_expired"),),
                ),
                api_call_count=1,
                errors=(),
                hydrated_ref_ids=(("101", "work_item"),),
                failed_ref_ids=(),
            )

    class _SignalExtractor:
        def extract(self, resources, program_id):
            del resources, program_id
            return ExtractionResult(
                channel="ado",
                signals=(),
                trajectory_points=(),
                side_artifacts={},
                errors=(
                    IntegrationError(
                        source="ado",
                        stage="extract",
                        retryable=False,
                        message="extractor failed",
                    ),
                ),
            )

    binding = ChannelBinding(
        config=ChannelConfig(channel="ado", enabled=True, discovery_threshold_hours=24, ttl_days=30),
        discovery_provider=_DiscoveryProvider(),
        hydration_provider=_HydrationProvider(),
        signal_extractor=_SignalExtractor(),
        discovery_config=object(),
        hydration_config=object(),
    )

    errors: list[IntegrationError] = []
    discovery_results: list[DiscoveryResult] = []
    channel_outcomes = []
    append_or_suppress_alert(
        program_id="demo",
        category="channel_budget_exceeded",
        entity_type="channel",
        entity_id="ado",
        severity="warn",
        message="ADO hydration exceeded its execution budget.",
        next_command="vertex cockpit show --program demo",
        programs_root=tmp_path,
    )
    hydration_result, extraction_result, delta = channel_runtime.run_channel_with_extraction(
        binding,
        store,
        program_id="demo",
        since=current_time - timedelta(days=14),
        verified_at=current_time,
        run_ctx=RunContext(),
        integration_error_sink=errors,
        discovery_result_sink=discovery_results,
        channel_outcome_sink=channel_outcomes,
        programs_root=tmp_path,
    )

    assert hydration_result is not None
    assert extraction_result is not None
    assert delta is not None
    assert extraction_result.errors[0].message == "extractor failed"
    assert len(errors) == 1
    assert errors[0].stage == "extract"
    assert errors[0].message == "extractor failed"
    assert errors[0].retryable is False
    assert discovery_results[0].query_results[0].membership_ids == ("101",)
    assert channel_outcomes[0].channel == "ado"
    assert channel_outcomes[0].degraded is False
    assert channel_outcomes[0].ado_call_count == 1
    assert channel_outcomes[0].checkpoint_reseed_reasons == (
        (101, "anti_entropy_expired"),
    )
    assert read_alerts("demo", programs_root=tmp_path) == ()


def test_run_channel_accept_shrinkage_prints_classified_removals(tmp_path: Path) -> None:
    # Sec 4.4: when --accept-shrinkage bypasses a would-be-blocked shrinkage
    # guard, run_channel must surface exactly what was removed via the same
    # operator-visible integration-error channel the guard-blocked path uses,
    # rather than silently applying the reduced registry.
    from src.core.channel_registry_store import ChannelRegistryStore
    from src.core.integration_types import (
        ChannelBinding,
        ChannelConfig,
        ChannelRegistration,
        DiscoveredRef,
        DiscoveryCompleteness,
        DiscoveryResult,
        HydrationResult,
        IntegrationError,
        RegistrationBinding,
        RegistrationStatus,
        RunContext,
    )

    current_time = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    store = ChannelRegistryStore(tmp_path / "demo" / "channel_registry.sqlite3", "demo")

    def _registration(ref_id: str) -> ChannelRegistration:
        return ChannelRegistration(
            channel="ado",
            program_id="demo",
            provider_instance_id="default",
            ref_id=ref_id,
            ref_kind="work_item",
            status=RegistrationStatus.ACTIVE,
            first_discovered_at=current_time,
            last_seen_at=current_time,
        )

    def _discovered(ref_id: str) -> DiscoveredRef:
        return DiscoveredRef(
            registration=_registration(ref_id),
            bindings=(
                RegistrationBinding(
                    workstream_id="demo.slice",
                    scope_id="scope",
                    source_type="wiql_saved_query",
                    confidence=1.0,
                    confidence_source="wiql_saved_query",
                ),
            ),
        )

    def _discovery_result(*ref_ids: str) -> DiscoveryResult:
        return DiscoveryResult(
            channel="ado",
            program_id="demo",
            discovered_refs=tuple(_discovered(ref_id) for ref_id in ref_ids),
            completeness=DiscoveryCompleteness.FULL,
            scope_statuses={},
            scope_state_updates={},
            errors=(),
            computed_at=current_time,
        )

    # Seed the registry with 6 active registrations so that discovering only
    # one of them next cycle trips the default shrinkage guard (>30% removed,
    # >=5 removed).
    store.apply_discovery_result(_discovery_result(*(str(ref_id) for ref_id in range(100, 106))))

    class _DiscoveryProvider:
        def discover(self, program_id, config, existing, run_ctx=None):
            del config, existing, run_ctx
            return _discovery_result("100")

    class _HydrationProvider:
        def hydrate(self, registrations, since, program_id, config, mode=None, run_ctx=None):
            del registrations, since, program_id, config, mode, run_ctx
            return HydrationResult(
                channel="ado", resources=object(), api_call_count=0, errors=(),
                hydrated_ref_ids=(), failed_ref_ids=(),
            )

    binding = ChannelBinding(
        config=ChannelConfig(channel="ado", enabled=True, discovery_threshold_hours=24, ttl_days=30),
        discovery_provider=_DiscoveryProvider(),
        hydration_provider=_HydrationProvider(),
        signal_extractor=object(),
        discovery_config=object(),
        hydration_config=object(),
    )

    errors: list[IntegrationError] = []
    hydration_result, delta = channel_runtime.run_channel(
        binding,
        store,
        program_id="demo",
        since=current_time - timedelta(days=14),
        verified_at=current_time,
        run_ctx=RunContext(accept_shrinkage=True),
        integration_error_sink=errors,
    )

    assert delta is not None
    assert delta.is_shrinkage_guarded()
    assert store.registration_count("ado") == 1
    assert len(errors) == 1
    assert errors[0].stage == "discovery"
    assert errors[0].message.startswith("Shrinkage accepted: 83% reduction (5 removed): ")
    for ref_id in ("101", "102", "103", "104", "105"):
        assert f"work_item:{ref_id}" in errors[0].message


def _base_binding_and_store(tmp_path: Path, *, current_time: datetime, resources):
    from src.core.channel_registry_store import ChannelRegistryStore
    from src.core.integration_types import (
        ChannelBinding,
        ChannelConfig,
        DiscoveryCompleteness,
        DiscoveryResult,
        ExtractionResult,
        HydrationResult,
        RunContext,
    )

    store = ChannelRegistryStore(tmp_path / "demo" / "channel_registry.sqlite3", "demo")

    class _DiscoveryProvider:
        def discover(self, program_id, config, existing, run_ctx=None):
            del config, existing, run_ctx
            return DiscoveryResult(
                channel="ado", program_id=program_id, discovered_refs=(),
                completeness=DiscoveryCompleteness.FULL, scope_statuses={}, scope_state_updates={},
                errors=(), computed_at=current_time,
            )

    class _HydrationProvider:
        def hydrate(self, registrations, since, program_id, config, mode=None, run_ctx=None):
            del registrations, since, program_id, config, mode, run_ctx
            return HydrationResult(
                channel="ado", resources=resources, api_call_count=1, errors=(),
                hydrated_ref_ids=(("101", "work_item"), ("102", "work_item")),
                failed_ref_ids=(),
            )

    class _SignalExtractor:
        def extract(self, resources, program_id):
            del resources, program_id
            return ExtractionResult(channel="ado", signals=(), trajectory_points=(), side_artifacts={}, errors=())

    binding = ChannelBinding(
        config=ChannelConfig(channel="ado", enabled=True, discovery_threshold_hours=24, ttl_days=30),
        discovery_provider=_DiscoveryProvider(),
        hydration_provider=_HydrationProvider(),
        signal_extractor=_SignalExtractor(),
        discovery_config=object(),
        hydration_config=object(),
    )
    return binding, store, RunContext()


def test_run_channel_with_extraction_computes_relation_scope_from_hydrated_ref_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ADF-W2.2: scope_item_ids must be derived from hydrated_ref_ids (the
    # work items actually queried this cycle), not left None/unset, so the
    # closure logic can distinguish real removal evidence from "not fetched."
    from src.core.integration_types import ADOHydrationOutput, RelationKind, RelationTargetKind, WorkItemRelation

    current_time = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    relation = WorkItemRelation(
        source_work_item_id=101, relation_kind=RelationKind.SUCCESSOR, target_kind=RelationTargetKind.WORK_ITEM,
        target_id="500", target_type="Task", target_title="Downstream", direction="forward",
        rel_type_name="System.LinkTypes.DependencySuccessor",
    )
    resources = ADOHydrationOutput(work_items=(), relations=(relation,))
    binding, store, run_ctx = _base_binding_and_store(tmp_path, current_time=current_time, resources=resources)

    captured: dict[str, object] = {}

    def _fake_sync(program_id, relations, *, scope_item_ids, item_lookup=None, programs_root):
        captured["relations"] = relations
        captured["scope_item_ids"] = scope_item_ids
        captured["item_lookup"] = item_lookup

    monkeypatch.setattr(channel_runtime, "_sync_relation_dependencies_best_effort", _fake_sync)

    channel_runtime.run_channel_with_extraction(
        binding, store, program_id="demo", since=current_time - timedelta(days=14),
        verified_at=current_time, run_ctx=run_ctx, programs_root=tmp_path,
    )

    assert captured["relations"] == (relation,)
    assert captured["scope_item_ids"] == frozenset({101, 102})
    assert captured["item_lookup"] == {}


def test_run_channel_with_extraction_builds_owner_workstream_lookup_from_work_items(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # RELO-W2.7: the item_lookup passed to the sync must reflect the real
    # hydrated WorkItem owner/workstream, not an all-None placeholder.
    from src.core.dependency_graph import RelationItemContext
    from src.core.integration_types import ADOHydrationOutput, RelationKind, RelationTargetKind, WorkItemRelation

    current_time = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    relation = WorkItemRelation(
        source_work_item_id=101, relation_kind=RelationKind.SUCCESSOR, target_kind=RelationTargetKind.WORK_ITEM,
        target_id="500", target_type="Task", target_title="Downstream", direction="forward",
        rel_type_name="System.LinkTypes.DependencySuccessor",
    )
    owned_item = WorkItem(
        id=101, type="Feature", title="Owned", state="Active", assigned_to="  Alice.Example  ",
        assigned_to_email="alice@example.com", area_path="One\\Demo", iteration_path="One\\Iteration",
        target_date=None, risk_level=RiskLevel.UNKNOWN, tags=[],
        custom_fields={"workstream_ids": ("demo.slice", "demo.other")}, fetched_at=current_time,
    )
    unowned_item = WorkItem(
        id=102, type="Feature", title="Unowned", state="Active", assigned_to=None,
        assigned_to_email=None, area_path="One\\Demo", iteration_path="One\\Iteration",
        target_date=None, risk_level=RiskLevel.UNKNOWN, tags=[],
        custom_fields={}, fetched_at=current_time,
    )
    resources = ADOHydrationOutput(work_items=(owned_item, unowned_item), relations=(relation,))
    binding, store, run_ctx = _base_binding_and_store(tmp_path, current_time=current_time, resources=resources)

    captured: dict[str, object] = {}

    def _fake_sync(program_id, relations, *, scope_item_ids, item_lookup=None, programs_root):
        del program_id, relations, scope_item_ids, programs_root
        captured["item_lookup"] = item_lookup

    monkeypatch.setattr(channel_runtime, "_sync_relation_dependencies_best_effort", _fake_sync)

    channel_runtime.run_channel_with_extraction(
        binding, store, program_id="demo", since=current_time - timedelta(days=14),
        verified_at=current_time, run_ctx=run_ctx, programs_root=tmp_path,
    )

    item_lookup = captured["item_lookup"]
    assert item_lookup[101] == RelationItemContext(owner_alias="alice.example", workstream_id="demo.slice")
    assert item_lookup[102] == RelationItemContext(owner_alias=None, workstream_id=None)


def test_run_channel_with_extraction_syncs_on_genuinely_empty_relations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ADF-W2.2: an ADO cycle that legitimately returns zero relations must
    # still trigger the sync (so a real all-relations-removed case actually
    # closes stale facts) -- the prior `if relations:` truthiness guard
    # silently skipped this.
    from src.core.integration_types import ADOHydrationOutput

    current_time = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    resources = ADOHydrationOutput(work_items=(), relations=())
    binding, store, run_ctx = _base_binding_and_store(tmp_path, current_time=current_time, resources=resources)

    calls: list[object] = []
    monkeypatch.setattr(
        channel_runtime, "_sync_relation_dependencies_best_effort",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    channel_runtime.run_channel_with_extraction(
        binding, store, program_id="demo", since=current_time - timedelta(days=14),
        verified_at=current_time, run_ctx=run_ctx, programs_root=tmp_path,
    )

    assert len(calls) == 1


def test_run_channel_with_extraction_skips_sync_for_non_ado_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A resources type with no `relations` attribute at all (e.g. a non-ADO
    # channel) must never trigger the sync.
    current_time = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)

    class _NonAdoResources:
        pass

    binding, store, run_ctx = _base_binding_and_store(tmp_path, current_time=current_time, resources=_NonAdoResources())

    calls: list[object] = []
    monkeypatch.setattr(
        channel_runtime, "_sync_relation_dependencies_best_effort",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    channel_runtime.run_channel_with_extraction(
        binding, store, program_id="demo", since=current_time - timedelta(days=14),
        verified_at=current_time, run_ctx=run_ctx, programs_root=tmp_path,
    )

    assert calls == []


@pytest.mark.parametrize(
    ("configured_instance", "force_discovery", "dry_run", "failed", "legacy_result"),
    [
        (None, False, False, False, False),
        (None, False, False, True, False),
        (None, True, False, False, False),
        (None, True, True, True, False),
        ("", False, False, False, False),
        ("default", False, False, False, False),
        ("instance-a", False, False, False, False),
        ("instance-a", True, False, True, False),
        ("instance-a", True, False, False, True),
    ],
)
def test_run_channel_isolates_provider_instance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    configured_instance: str | None,
    force_discovery: bool,
    dry_run: bool,
    failed: bool,
    legacy_result: bool,
) -> None:
    from dataclasses import replace

    from src.core.channel_registry_store import ChannelRegistryStore
    from src.core.integration_types import (
        ADOHydrationOutput, ChannelBinding, ChannelConfig, ChannelRegistration, DiscoveredRef,
        DiscoveryCompleteness, DiscoveryResult, HydrationResult, RegistrationBinding,
        RegistrationStatus, RunContext,
    )

    now = datetime.now(timezone.utc)
    selected = configured_instance or "default"
    other = "instance-a" if selected == "default" else "default"
    store = ChannelRegistryStore(tmp_path / "registry.sqlite3", "demo")
    refs = {}
    for instance, distinct_id in (("default", "102"), ("instance-a", "201")):
        refs[instance] = tuple(
            DiscoveredRef(
                registration=ChannelRegistration(
                    channel="ado", program_id="demo", provider_instance_id=instance,
                    ref_id=ref_id, ref_kind="work_item", status=RegistrationStatus.ACTIVE,
                    first_discovered_at=now, last_seen_at=now,
                ),
                bindings=(RegistrationBinding(
                    workstream_id=f"demo.{instance}", scope_id=f"scope-{instance}",
                    source_type="wiql_saved_query", confidence=1.0, confidence_source="wiql_saved_query",
                ),),
            )
            for ref_id in ("101", distinct_id)
        )
        store.apply_discovery_result(DiscoveryResult(
            channel="ado", program_id="demo", discovered_refs=refs[instance],
            completeness=DiscoveryCompleteness.FULL, scope_statuses={}, scope_state_updates={},
            errors=(), computed_at=now, provider_instance_id=instance,
        ))

    before_other = store.all_registrations("ado", provider_instance_id=other)
    discoveries = []
    hydrations = []

    class _DiscoveryProvider:
        def discover(self, program_id, config, existing, run_ctx=None):
            del config, run_ctx
            discoveries.append(existing)
            discovered_refs = refs[selected]
            if legacy_result:
                discovered_refs = tuple(
                    replace(ref, registration=replace(ref.registration, provider_instance_id="default"))
                    for ref in discovered_refs
                )
            return DiscoveryResult(
                channel="ado", program_id=program_id, discovered_refs=discovered_refs,
                completeness=DiscoveryCompleteness.FULL, scope_statuses={}, scope_state_updates={},
                errors=(), computed_at=now, provider_instance_id="default" if legacy_result else selected,
            )

    class _HydrationProvider:
        def hydrate(self, registrations, since, program_id, config, mode=None, run_ctx=None):
            del since, program_id, config, mode, run_ctx
            hydrations.append(registrations)
            item = WorkItem(
                id=101, type="Feature", title="Hydrated", state="Active", assigned_to=None,
                assigned_to_email=None, area_path="One\\Demo", iteration_path="One\\Iteration",
                target_date=None, risk_level=RiskLevel.UNKNOWN, tags=[], custom_fields={}, fetched_at=now,
            )
            return HydrationResult(
                channel="ado",
                resources=ADOHydrationOutput(work_items=() if failed else (item,), freshness_items=()),
                api_call_count=1, errors=(),
                hydrated_ref_ids=() if failed else (("101", "work_item"),),
                failed_ref_ids=(("101", "work_item"),) if failed else (),
            )

    binding = ChannelBinding(
        config=ChannelConfig(
            channel="ado", enabled=True, discovery_threshold_hours=24, ttl_days=None,
            extra=None if configured_instance is None else {"instance_id": configured_instance},
        ),
        discovery_provider=_DiscoveryProvider(), hydration_provider=_HydrationProvider(),
        signal_extractor=object(), discovery_config=object(), hydration_config=object(),
    )
    monkeypatch.setattr(channel_runtime, "load_arch_data_fix", lambda *args, **kwargs: None)
    errors = []
    hydration, _ = channel_runtime.run_channel(
        binding, store, program_id="demo", since=now - timedelta(days=14),
        verified_at=now + timedelta(minutes=1),
        run_ctx=RunContext(force_discovery=force_discovery, dry_run=dry_run),
        integration_error_sink=errors, programs_root=tmp_path,
    )

    assert len(discoveries) == int(force_discovery)
    if discoveries:
        assert {registration.provider_instance_id for registration in discoveries[0]} == {selected}
    assert store.all_registrations("ado", provider_instance_id=other) == before_other
    assert len(hydrations) == 1
    assert {(registration.provider_instance_id, registration.ref_id) for registration in hydrations[0]} == {
        (selected, ref.registration.ref_id) for ref in refs[selected]
    }
    assert next(registration for registration in hydrations[0] if registration.ref_id == "101").workstream_ids == (
        f"demo.{selected}",
    )
    assert hydration is not None
    if not failed:
        assert hydration.resources.work_items[0].custom_fields["workstream_ids"] == (f"demo.{selected}",)
    selected_registration = next(
        registration for registration in store.all_registrations("ado", provider_instance_id=selected)
        if registration.ref_id == "101"
    )
    assert selected_registration.last_verified_at == (
        now + timedelta(minutes=1) if not failed and not dry_run else None
    )
    assert selected_registration.consecutive_hydration_failures == int(failed and not dry_run)


def test_run_channel_default_provider_instance_discovery_ignores_other_freshness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from src.core.channel_registry_store import ChannelRegistryStore
    from src.core.integration_types import (
        ADOHydrationOutput, ChannelConfig, ChannelRegistration, DiscoveredRef, DiscoveryCompleteness,
        DiscoveryResult, HydrationResult, RegistrationStatus, RunContext,
    )

    now = datetime.now(timezone.utc)
    store = ChannelRegistryStore(tmp_path / "registry.sqlite3", "demo")
    store.apply_discovery_result(DiscoveryResult(
        channel="ado", program_id="demo",
        discovered_refs=(DiscoveredRef(registration=ChannelRegistration(
            channel="ado", program_id="demo", provider_instance_id="instance-a", ref_id="101",
            ref_kind="work_item", status=RegistrationStatus.ACTIVE, first_discovered_at=now, last_seen_at=now,
        ), bindings=()),),
        completeness=DiscoveryCompleteness.FULL,
        scope_statuses={}, scope_state_updates={}, errors=(), computed_at=now, provider_instance_id="instance-a",
    ))
    discoveries = []

    def discover(program_id, config, existing, run_ctx=None):
        del config, run_ctx
        discoveries.append(existing)
        return DiscoveryResult(
            channel="ado", program_id=program_id, discovered_refs=(), completeness=DiscoveryCompleteness.FULL,
            scope_statuses={}, scope_state_updates={}, errors=(), computed_at=now,
        )

    binding = SimpleNamespace(
        config=ChannelConfig(channel="ado", enabled=True, discovery_threshold_hours=24, ttl_days=None),
        discovery_provider=SimpleNamespace(discover=discover),
        hydration_provider=SimpleNamespace(hydrate=lambda *args, **kwargs: HydrationResult(
            channel="ado", resources=ADOHydrationOutput(work_items=(), freshness_items=()),
            api_call_count=0, errors=(), hydrated_ref_ids=(), failed_ref_ids=(),
        )),
        discovery_config=object(), hydration_config=object(),
    )
    monkeypatch.setattr(channel_runtime, "load_arch_data_fix", lambda *args, **kwargs: None)
    channel_runtime.run_channel(
        binding, store, program_id="demo", since=now, verified_at=now, run_ctx=RunContext(),
        integration_error_sink=[], programs_root=tmp_path,
    )

    assert discoveries == [()]


@pytest.mark.parametrize(
    ("selected", "returned", "result_kind"),
    [
        ("default", "instance-a", "declared"),
        ("instance-a", "instance-b", "declared"),
        ("default", "instance-a", "ref"),
        ("default", "instance-a", "mixed"),
    ],
)
def test_run_channel_rejects_foreign_discovery_provider_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selected: str, returned: str, result_kind: str,
) -> None:
    from dataclasses import replace
    from types import SimpleNamespace

    from src.core.channel_registry_store import ChannelRegistryStore
    from src.core.integration_types import (
        ADOHydrationOutput, ChannelConfig, ChannelRegistration, DiscoveredRef, DiscoveryCompleteness,
        DiscoveryResult, HydrationResult, RegistrationBinding, RegistrationStatus, RunContext,
    )

    now = datetime.now(timezone.utc)
    store = ChannelRegistryStore(tmp_path / "registry.sqlite3", "demo")
    refs = {}
    for instance in (selected, returned):
        ref = DiscoveredRef(
            registration=ChannelRegistration(
                channel="ado", program_id="demo", provider_instance_id=instance, ref_id="101",
                ref_kind="work_item", status=RegistrationStatus.ACTIVE, first_discovered_at=now, last_seen_at=now,
            ),
            bindings=(RegistrationBinding(
                workstream_id=f"demo.{instance}", scope_id=instance, source_type="wiql_saved_query",
                confidence=1.0, confidence_source="wiql_saved_query",
            ),),
        )
        store.apply_discovery_result(DiscoveryResult(
            channel="ado", program_id="demo", discovered_refs=(ref,), completeness=DiscoveryCompleteness.FULL,
            scope_statuses={}, scope_state_updates={}, errors=(), computed_at=now, provider_instance_id=instance,
        ))
        refs[instance] = ref
    before_other = store.all_registrations("ado", provider_instance_id=returned)
    hydrations = []

    def hydrate(registrations, *args, **kwargs):
        hydrations.append(registrations)
        return HydrationResult(
            channel="ado", resources=ADOHydrationOutput(work_items=(), freshness_items=()), api_call_count=0,
            errors=(), hydrated_ref_ids=(), failed_ref_ids=(),
        )

    discovered_refs = ()
    if result_kind in {"ref", "mixed"}:
        discovered_refs = (refs[returned],)
    if result_kind == "mixed":
        discovered_refs += (replace(
            refs[returned], registration=replace(refs[returned].registration, provider_instance_id="instance-b"),
        ),)
    binding = SimpleNamespace(
        config=ChannelConfig(
            channel="ado", enabled=True, discovery_threshold_hours=24, ttl_days=None,
            extra=None if selected == "default" else {"instance_id": selected},
        ),
        discovery_provider=SimpleNamespace(discover=lambda *args, **kwargs: DiscoveryResult(
            channel="ado", program_id="demo", discovered_refs=discovered_refs, completeness=DiscoveryCompleteness.FULL,
            scope_statuses={}, scope_state_updates={}, errors=(), computed_at=now,
            provider_instance_id=returned if result_kind == "declared" else "default",
        )),
        hydration_provider=SimpleNamespace(hydrate=hydrate),
        discovery_config=object(), hydration_config=object(),
    )
    monkeypatch.setattr(channel_runtime, "load_arch_data_fix", lambda *args, **kwargs: None)
    errors, outcomes, captures = [], [], []
    hydration, delta = channel_runtime.run_channel(
        binding, store, program_id="demo", since=now, verified_at=now, run_ctx=RunContext(force_discovery=True),
        integration_error_sink=errors, channel_outcome_sink=outcomes, discovery_result_sink=captures,
        programs_root=tmp_path,
    )

    assert hydration is not None
    assert delta is None
    assert captures == []
    assert len(hydrations) == 1
    assert {registration.provider_instance_id for registration in hydrations[0]} == {selected}
    assert store.all_registrations("ado", provider_instance_id=returned) == before_other
    assert len(errors) == 1 and errors[0].stage == "discovery"
    assert "provider instance" in errors[0].message.lower()
    assert returned not in errors[0].message
    assert len(outcomes) == 1 and outcomes[0].degraded


@pytest.mark.parametrize("gather_v2_enabled", [False, True])
@pytest.mark.parametrize("selected", ["default", "instance-a"])
@pytest.mark.parametrize("deferred", [False, True])
def test_uil_ado_loader_isolates_provider_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gather_v2_enabled: bool, selected: str, deferred: bool,
) -> None:
    from types import SimpleNamespace

    from src.commands import gather
    from src.core.channel_registry_store import ChannelRegistryStore
    from src.core.integration_types import (
        ADOHydrationOutput, ChannelConfig, ChannelRegistration, DiscoveredRef, DiscoveryCompleteness,
        DiscoveryResult, HydrationResult, RegistrationBinding, RegistrationStatus,
    )
    from src.core.models_v2 import Program
    from src.core.program_paths import get_channel_registry_path

    now = datetime.now(timezone.utc)
    store = ChannelRegistryStore(get_channel_registry_path("demo", programs_root=tmp_path), "demo")
    for instance in ("default", "instance-a"):
        ref = DiscoveredRef(
            registration=ChannelRegistration(
                channel="ado", program_id="demo", provider_instance_id=instance, ref_id="101",
                ref_kind="work_item", status=RegistrationStatus.ACTIVE, first_discovered_at=now, last_seen_at=now,
            ),
            bindings=(RegistrationBinding(
                workstream_id=f"demo.{instance}", scope_id=instance, source_type="wiql_saved_query",
                confidence=1.0, confidence_source="wiql_saved_query",
            ),),
        )
        store.apply_discovery_result(DiscoveryResult(
            channel="ado", program_id="demo", discovered_refs=(ref,), completeness=DiscoveryCompleteness.FULL,
            scope_statuses={}, scope_state_updates={}, errors=(), computed_at=now, provider_instance_id=instance,
        ))
    other = "instance-a" if selected == "default" else "default"
    before_selected = store.all_registrations("ado", provider_instance_id=selected)
    before_other = store.all_registrations("ado", provider_instance_id=other)
    hydrated = []
    item = WorkItem(
        id=101, type="Feature", title="Hydrated", state="Active", assigned_to=None, assigned_to_email=None,
        area_path="One\\Demo", iteration_path="One\\Iteration", target_date=None, risk_level=RiskLevel.UNKNOWN,
        tags=[], custom_fields={}, fetched_at=now,
    )

    def hydrate(registrations, *args, **kwargs):
        hydrated.append(registrations)
        return HydrationResult(
            channel="ado", resources=ADOHydrationOutput(work_items=(item,), freshness_items=(item,)),
            api_call_count=1, errors=(), hydrated_ref_ids=(("101", "work_item"),), failed_ref_ids=(),
        )

    binding = SimpleNamespace(
        config=ChannelConfig(
            channel="ado", enabled=True, discovery_threshold_hours=24, ttl_days=None,
            extra=None if selected == "default" else {"instance_id": selected},
        ),
        discovery_provider=SimpleNamespace(),
        hydration_provider=SimpleNamespace(hydrate=hydrate),
        discovery_config=object(), hydration_config=object(),
    )
    monkeypatch.setattr(channel_runtime, "load_arch_data_fix", lambda *args, **kwargs: None)
    monkeypatch.setattr(channel_runtime, "_resolve_channel_budget_alert_best_effort", lambda *args, **kwargs: None)
    monkeypatch.delenv("VERTEX_UIL_FORCE_DISCOVERY", raising=False)
    monkeypatch.delenv("VERTEX_UIL_ACCEPT_SHRINKAGE", raising=False)
    deferred_verifications = [] if deferred else None
    items, freshness, calls = gather._load_ado_items_via_uil(
        Program(schema_version="2.0", id="demo", name="Demo"), (), now + timedelta(minutes=1),
        since=now, programs_root=tmp_path, binding=binding, integration_error_sink=[],
        gather_v2_enabled=gather_v2_enabled, deferred_verifications=deferred_verifications,
    )

    assert len(hydrated) == 1
    assert {registration.provider_instance_id for registration in hydrated[0]} == {selected}
    assert items[0].custom_fields["workstream_ids"] == (f"demo.{selected}",)
    assert freshness == items and calls == 1
    if deferred_verifications is not None:
        assert store.all_registrations("ado", provider_instance_id=selected) == before_selected
        assert len(deferred_verifications) == 1
        deferred_verifications[0]()
    assert store.all_registrations("ado", provider_instance_id=selected)[0].last_verified_at == now + timedelta(minutes=1)
    assert store.all_registrations("ado", provider_instance_id=other) == before_other
