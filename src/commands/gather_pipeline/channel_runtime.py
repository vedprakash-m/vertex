from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any

from src.core.channel_registry_store import (
    ChannelRegistryStore,
    ShrinkageGuardError,
    compute_registry_delta,
    normalize_discovery_result_provider_instance,
)
from src.core.gather_channel_support import append_integration_error_once, binding_provider_instance_id
from src.core.exceptions import QueryError
from src.core.integration_types import HydrationMode, RunContext
from src.core.models_v2 import IntegrationError
from src.commands.gather_pipeline.support import enrich_resources, sanitize_discovery_result


def run_channel(
    binding: Any,
    store: ChannelRegistryStore,
    *,
    program_id: str,
    since: datetime,
    verified_at: datetime,
    run_ctx: RunContext,
    integration_error_sink: list[IntegrationError] | None = None,
) -> tuple[Any | None, Any | None]:
    channel = binding.config.channel
    provider_instance_id = binding_provider_instance_id(binding)
    delta = None
    if not run_ctx.dry_run:
        store.ensure_status_transitions(channel)
    should_discover = run_ctx.force_discovery or store.is_discovery_stale(
        channel,
        binding.config.discovery_threshold_hours,
        provider_instance_id=provider_instance_id,
    )
    if should_discover:
        try:
            discovery_result = binding.discovery_provider.discover(
                program_id,
                binding.discovery_config,
                store.active_registrations(channel, provider_instance_id=provider_instance_id),
                run_ctx=run_ctx,
            )
            discovery_result = sanitize_discovery_result(discovery_result, binding.config)
            discovery_result = normalize_discovery_result_provider_instance(
                discovery_result,
                expected_provider_instance_id=provider_instance_id,
            )
            for error in discovery_result.errors:
                append_integration_error_once(
                    integration_error_sink,
                    source=error.source,
                    stage=error.stage,
                    error=error.message,
                )
            if run_ctx.dry_run:
                delta = compute_registry_delta(
                    store.load_discovered_refs(channel, provider_instance_id=provider_instance_id),
                    discovery_result,
                )
            else:
                try:
                    delta = store.apply_discovery_result(
                        discovery_result,
                        ttl_days=binding.config.ttl_days,
                        accept_shrinkage=run_ctx.accept_shrinkage,
                    )
                except ShrinkageGuardError as error:
                    delta = error.computed_delta
                    for scope_id, status in discovery_result.scope_statuses.items():
                        store.record_scope_status(
                            channel,
                            scope_id,
                            status,
                            provider_instance_id=provider_instance_id or "default",
                            recorded_at=discovery_result.computed_at,
                        )
                    append_integration_error_once(
                        integration_error_sink,
                        source=channel,
                        stage="discovery",
                        error=f"Shrinkage guard: {error.shrinkage_pct:.0%} reduction",
                    )
                    # Preserve the legacy behavior: shrinkage blocks the registry update,
                    # but hydration still runs against the pre-shrinkage registrations.
        except (QueryError, RuntimeError, ValueError) as error:
            append_integration_error_once(
                integration_error_sink,
                source=channel,
                stage="discovery",
                error=str(error),
            )
    try:
        registrations = store.pullable_registrations(channel, provider_instance_id=provider_instance_id)
        workstream_map = store.get_workstream_map(
            channel,
            tuple((registration.ref_id, registration.ref_kind) for registration in registrations),
            provider_instance_id=provider_instance_id,
        )
        hydration_result = binding.hydration_provider.hydrate(
            registrations,
            since,
            program_id,
            binding.hydration_config,
            mode=HydrationMode.FULL,
            run_ctx=run_ctx,
        )
        hydration_result = replace(
            hydration_result,
            resources=enrich_resources(hydration_result.resources, workstream_map),
        )
    except (QueryError, RuntimeError, ValueError) as error:
        append_integration_error_once(
            integration_error_sink,
            source=channel,
            stage="hydration",
            error=str(error),
        )
        return None, delta
    if not run_ctx.dry_run and hydration_result.hydrated_ref_ids:
        store.mark_verified(
            channel,
            hydration_result.hydrated_ref_ids,
            verified_at=verified_at,
            provider_instance_id=provider_instance_id,
        )
    if hydration_result.failed_ref_ids:
        if not run_ctx.dry_run:
            store.mark_hydration_failed(
                channel,
                hydration_result.failed_ref_ids,
                provider_instance_id=provider_instance_id,
            )
        for ref_id, ref_kind in hydration_result.failed_ref_ids:
            append_integration_error_once(
                integration_error_sink,
                source=channel,
                stage="hydration",
                error=f"Failed to hydrate {ref_kind}:{ref_id}",
            )
    for hydration_error in hydration_result.errors:
        append_integration_error_once(
            integration_error_sink,
            source=hydration_error.source,
            stage=hydration_error.stage,
            error=hydration_error.message,
        )
    return hydration_result, delta


def run_channel_with_extraction(
    binding: Any,
    store: ChannelRegistryStore,
    *,
    program_id: str,
    since: datetime,
    verified_at: datetime,
    run_ctx: RunContext,
    integration_error_sink: list[IntegrationError] | None = None,
) -> tuple[Any | None, Any | None, Any | None]:
    hydration_result, delta = run_channel(
        binding,
        store,
        program_id=program_id,
        since=since,
        verified_at=verified_at,
        run_ctx=run_ctx,
        integration_error_sink=integration_error_sink,
    )
    if hydration_result is None:
        return None, None, delta
    extraction_result = binding.signal_extractor.extract(hydration_result.resources, program_id)
    for error in extraction_result.errors:
        append_integration_error_once(
            integration_error_sink,
            source=error.source,
            stage=error.stage,
            error=error.message,
        )
    return hydration_result, extraction_result, delta
