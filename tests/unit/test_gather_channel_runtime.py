from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.commands.gather_pipeline import channel_runtime
from src.core.models import RiskLevel, WorkItem


def test_run_channel_with_extraction_surfaces_extractor_errors(tmp_path: Path) -> None:
    from src.core.channel_registry_store import ChannelRegistryStore
    from src.core.integration_types import (
        ADOHydrationOutput,
        ChannelBinding,
        ChannelConfig,
        ChannelRegistration,
        DiscoveredRef,
        DiscoveryCompleteness,
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
        tags=["RAMPP1"],
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
            )

    class _HydrationProvider:
        def hydrate(self, registrations, since, program_id, config, mode=None, run_ctx=None):
            del registrations, since, program_id, config, mode, run_ctx
            return HydrationResult(
                channel="ado",
                resources=ADOHydrationOutput(work_items=(item,), freshness_items=(item,)),
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
    hydration_result, extraction_result, delta = channel_runtime.run_channel_with_extraction(
        binding,
        store,
        program_id="demo",
        since=current_time - timedelta(days=14),
        verified_at=current_time,
        run_ctx=RunContext(),
        integration_error_sink=errors,
    )

    assert hydration_result is not None
    assert extraction_result is not None
    assert delta is not None
    assert extraction_result.errors[0].message == "extractor failed"
    assert len(errors) == 1
    assert errors[0].stage == "extract"
    assert errors[0].message == "extractor failed"


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

    def _fake_sync(program_id, relations, *, scope_item_ids, programs_root):
        captured["relations"] = relations
        captured["scope_item_ids"] = scope_item_ids

    monkeypatch.setattr(channel_runtime, "_sync_relation_dependencies_best_effort", _fake_sync)

    channel_runtime.run_channel_with_extraction(
        binding, store, program_id="demo", since=current_time - timedelta(days=14),
        verified_at=current_time, run_ctx=run_ctx, programs_root=tmp_path,
    )

    assert captured["relations"] == (relation,)
    assert captured["scope_item_ids"] == frozenset({101, 102})


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
