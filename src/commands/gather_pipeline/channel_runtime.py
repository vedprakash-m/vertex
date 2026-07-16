from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from src.core.adf_config import load_arch_data_fix
from src.core.alerts import append_or_suppress_alert
from src.core.channel_execution_policy import channel_execution_policy_for, run_under_channel_budget
from src.core.channel_registry_store import (
    ChannelRegistryStore,
    ShrinkageGuardError,
    compute_registry_delta,
    normalize_discovery_result_provider_instance,
)
from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.gather_channel_support import append_integration_error_once, binding_provider_instance_id
from src.core.exceptions import QueryError
from src.core.integration_types import HydrationMode, HydrationResult, RunContext
from src.core.models_v2 import IntegrationError
from src.commands.gather_pipeline.support import enrich_resources, sanitize_discovery_result


def _sync_relation_dependencies_best_effort(
    program_id: str, relations: Any, *, scope_item_ids: frozenset[int] | None, programs_root: Path
) -> None:
    """ADF-W4.4 (Section 8.10.3): convert typed ADO relations into
    AUTHORITATIVE_RELATION dependencies (fact-store only). Lazy-imported to
    keep Zone-B (this module) free of a hard Zone-A dependency edge at module
    load time and to avoid the cost when no relations exist. Best-effort: a
    fact-store write failure never breaks gather, matching every other
    best-effort side-effect emission in this codebase.

    ADF-W2.2: ``scope_item_ids`` is the set of work-item ids actually queried
    this cycle (today, every registration -- hydration is still full-mode --
    but this makes the closure logic safe for a future incremental relation
    fetch with zero change needed here when that lands).
    """
    try:
        from src.core.dependency_graph import sync_authoritative_relation_dependencies

        sync_authoritative_relation_dependencies(
            program_id, relations, scope_item_ids=scope_item_ids, programs_root=programs_root,
        )
    except (OSError, ValueError):
        # A fact-store I/O or schema failure is logged via the integration-
        # error sink elsewhere; here we swallow to keep gather resilient.
        pass


def _emit_channel_budget_alert_best_effort(
    *, program_id: str, channel: str, stage: str, degrade_reason: str | None, programs_root: Path
) -> None:
    """ADF-W5.8 (Section 8.2.5's "channel budget exceeded" category).
    Best-effort -- an alert-write failure must never break gather, matching
    every other best-effort alert emission in this codebase."""
    try:
        append_or_suppress_alert(
            program_id=program_id,
            category="channel_budget_exceeded",
            entity_type="channel",
            entity_id=channel,
            severity="warn",
            message=f"{channel} {stage} exceeded its execution budget ({degrade_reason or 'unknown reason'}).",
            next_command=f"vertex cockpit show --program {program_id}",
            programs_root=programs_root,
        )
    except Exception:
        pass


class _SkipDiscoveryApplication(Exception):
    """ADF-W1.4 internal control-flow signal: discovery was budget-degraded.

    Raised only to unwind out of the discovery try-block without falling
    into the generic (QueryError, RuntimeError, ValueError) handler below,
    which would otherwise log a second, redundant integration error for the
    same degradation already reported at the timeout site.
    """


def run_channel(
    binding: Any,
    store: ChannelRegistryStore,
    *,
    program_id: str,
    since: datetime,
    verified_at: datetime,
    run_ctx: RunContext,
    integration_error_sink: list[IntegrationError] | None = None,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[Any | None, Any | None]:
    channel = binding.config.channel
    provider_instance_id = binding_provider_instance_id(binding)
    delta = None
    # ADF-W1.4 (Section 8.3.1): bounded, non-blocking channel execution. An
    # unratified/unconfigured channel gets a safe conservative default policy
    # (see channel_execution_policy_for) rather than blocking indefinitely.
    try:
        adf_config = load_arch_data_fix(program_id, programs_root=programs_root)
    except Exception:
        adf_config = None
    policy = channel_execution_policy_for(channel, config=adf_config) if adf_config is not None else None
    if not run_ctx.dry_run:
        store.ensure_status_transitions(channel)
    should_discover = run_ctx.force_discovery or store.is_discovery_stale(
        channel,
        binding.config.discovery_threshold_hours,
        provider_instance_id=provider_instance_id,
    )
    if should_discover:
        try:
            if policy is not None:
                outcome = run_under_channel_budget(
                    lambda: binding.discovery_provider.discover(
                        program_id,
                        binding.discovery_config,
                        store.active_registrations(channel, provider_instance_id=provider_instance_id),
                        run_ctx=run_ctx,
                    ),
                    policy=policy,
                )
                if outcome.degraded:
                    # ADF-W1.4: skip discovery this run rather than block past
                    # budget. The registry's existing (last known good)
                    # registrations are left untouched -- hydration below
                    # still runs against them.
                    append_integration_error_once(
                        integration_error_sink,
                        source=channel,
                        stage="discovery",
                        error=f"Discovery exceeded {policy.per_attempt_timeout_seconds}s budget; skipped this run ({outcome.degrade_reason}).",
                    )
                    _emit_channel_budget_alert_best_effort(
                        program_id=program_id, channel=channel, stage="discovery",
                        degrade_reason=outcome.degrade_reason, programs_root=programs_root,
                    )
                    discovery_result = None
                else:
                    discovery_result = outcome.value
            else:
                discovery_result = binding.discovery_provider.discover(
                    program_id,
                    binding.discovery_config,
                    store.active_registrations(channel, provider_instance_id=provider_instance_id),
                    run_ctx=run_ctx,
                )
            if discovery_result is None:
                raise _SkipDiscoveryApplication()
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
        except _SkipDiscoveryApplication:
            pass
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
        if policy is not None:
            outcome = run_under_channel_budget(
                lambda: binding.hydration_provider.hydrate(
                    registrations,
                    since,
                    program_id,
                    binding.hydration_config,
                    mode=HydrationMode.FULL,
                    run_ctx=run_ctx,
                ),
                policy=policy,
            )
            if outcome.degraded:
                # ADF-W1.4 (Section 8.3.2): never a success-shaped empty
                # result -- return None so callers treat this exactly like
                # the existing hydration-exception path. The store is not
                # mutated here, so prior verified/hydrated state (the "last
                # known good snapshot") is left untouched.
                append_integration_error_once(
                    integration_error_sink,
                    source=channel,
                    stage="hydration",
                    error=f"Hydration exceeded {policy.per_attempt_timeout_seconds}s budget; degraded ({outcome.degrade_reason}).",
                )
                _emit_channel_budget_alert_best_effort(
                    program_id=program_id, channel=channel, stage="hydration",
                    degrade_reason=outcome.degrade_reason, programs_root=programs_root,
                )
                return None, delta
            hydration_result = cast(HydrationResult[Any], outcome.value)
        else:
            hydration_result = cast(HydrationResult[Any], binding.hydration_provider.hydrate(
                registrations,
                since,
                program_id,
                binding.hydration_config,
                mode=HydrationMode.FULL,
                run_ctx=run_ctx,
            ))
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
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[Any | None, Any | None, Any | None]:
    hydration_result, delta = run_channel(
        binding,
        store,
        program_id=program_id,
        since=since,
        verified_at=verified_at,
        run_ctx=run_ctx,
        integration_error_sink=integration_error_sink,
        programs_root=programs_root,
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
    # ADF-W4.4 (Section 8.10.3): convert typed ADO relations into
    # AUTHORITATIVE_RELATION dependencies. The relations live on the hydration
    # output (populated by ADOHydrationProvider in FULL mode); only the ADO
    # channel's resources type carries a ``relations`` attribute at all (it's
    # declared on ``ADOHydrationOutput`` specifically, not a shared base), so
    # ``getattr(..., None)`` correctly distinguishes "not ADO" (None) from
    # "ADO, genuinely zero relations this cycle" (``()``) -- checking
    # `is not None` (ADF-W2.2 fix) rather than truthiness means a real
    # all-relations-removed cycle now actually triggers closure, which the
    # prior `if relations:` guard silently skipped. Best-effort: a
    # fact-store write failure never breaks gather.
    relations = getattr(hydration_result.resources, "relations", None)
    if relations is not None:
        # ADF-W2.2: the work items whose relations were actually queried this
        # cycle -- today this is every hydrated work-item registration
        # (hydration is still full-mode), but reusing `hydrated_ref_ids`
        # rather than re-deriving it means this scope narrows automatically,
        # with no change here, whenever relation fetching itself becomes
        # incremental.
        scope_item_ids = frozenset(
            int(ref_id)
            for ref_id, ref_kind in hydration_result.hydrated_ref_ids
            if ref_kind == "work_item" and ref_id.isdigit()
        )
        _sync_relation_dependencies_best_effort(
            program_id, relations, scope_item_ids=scope_item_ids, programs_root=programs_root,
        )
    return hydration_result, extraction_result, delta
