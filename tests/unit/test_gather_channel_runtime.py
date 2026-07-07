from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

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
