from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from src.core.channel_registry_store import ChannelRegistryStore
from src.core.program_paths import get_channel_registry_path
from src.core.integration_types import RunContext
from src.core.kusto_client import build_live_kusto_query_executor
from src.core.kusto_query_loader import load_kpi_queries
from src.core.models import WorkItem
from src.core.models_v2 import KustoQuery, Program, Signal


def load_ado_items_via_uil(
    program: Program,
    as_of: datetime,
    *,
    since: datetime,
    programs_root: Path,
    binding: Any,
    integration_error_sink: list[Any] | None,
    env_flag_fn: Callable[[str], bool],
    run_channel_fn: Callable[..., tuple[Any | None, Any | None]],
    discovery_result_sink: list[Any] | None = None,
    channel_outcome_sink: list[Any] | None = None,
) -> tuple[tuple[WorkItem, ...], tuple[WorkItem, ...], int]:
    run_channel_kwargs: dict[str, Any] = {
        "program_id": program.id,
        "since": since,
        "verified_at": as_of,
        "run_ctx": _build_uil_run_context(env_flag_fn),
        "integration_error_sink": integration_error_sink,
    }
    if discovery_result_sink is not None:
        run_channel_kwargs["discovery_result_sink"] = discovery_result_sink
    if channel_outcome_sink is not None:
        run_channel_kwargs["channel_outcome_sink"] = channel_outcome_sink
    hydration_result, _ = run_channel_fn(
        binding,
        _build_uil_store(program.id, programs_root),
        **run_channel_kwargs,
    )
    if hydration_result is None:
        return (), (), 0
    return (
        hydration_result.resources.work_items,
        hydration_result.resources.freshness_items or hydration_result.resources.work_items,
        hydration_result.api_call_count,
    )


def load_signal_channel_via_uil(
    program: Program,
    as_of: datetime,
    *,
    programs_root: Path,
    binding: Any,
    integration_error_sink: list[Any] | None,
    env_flag_fn: Callable[[str], bool],
    run_channel_with_extraction_fn: Callable[..., tuple[Any | None, Any | None, Any | None]],
) -> tuple[tuple[Signal, ...], int]:
    hydration_result, extraction_result = _run_signal_channel_via_uil(
        program,
        as_of,
        programs_root=programs_root,
        binding=binding,
        integration_error_sink=integration_error_sink,
        env_flag_fn=env_flag_fn,
        run_channel_with_extraction_fn=run_channel_with_extraction_fn,
    )
    if hydration_result is None or extraction_result is None:
        return (), 0
    return extraction_result.signals, hydration_result.api_call_count


def load_kusto_signals_via_uil(
    program: Program,
    as_of: datetime,
    *,
    programs_root: Path,
    binding: Any,
    integration_error_sink: list[Any] | None,
    kusto_query_executor: Any | None,
    include_unvalidated: bool,
    query_state_sink: dict[str, dict[str, Any]] | None,
    previous_query_states: dict[str, dict[str, Any]] | None,
    env_flag_fn: Callable[[str], bool],
    run_channel_with_extraction_fn: Callable[..., tuple[Any | None, Any | None, Any | None]],
    record_kusto_query_state_fn: Callable[..., None],
) -> tuple[tuple[Signal, ...], int]:
    binding = build_kusto_gather_binding(
        binding,
        kusto_query_executor=kusto_query_executor,
        include_unvalidated=include_unvalidated,
    )
    hydration_result, extraction_result = _run_signal_channel_via_uil(
        program,
        as_of,
        programs_root=programs_root,
        binding=binding,
        integration_error_sink=integration_error_sink,
        env_flag_fn=env_flag_fn,
        run_channel_with_extraction_fn=run_channel_with_extraction_fn,
    )
    if hydration_result is None or extraction_result is None:
        return (), 0
    record_uil_kusto_query_states(
        program.id,
        binding=binding,
        hydration_result=hydration_result,
        as_of=as_of,
        query_state_sink=query_state_sink,
        previous_query_states=previous_query_states,
        include_unvalidated=include_unvalidated,
        record_kusto_query_state_fn=record_kusto_query_state_fn,
    )
    return extraction_result.signals, hydration_result.api_call_count


def build_kusto_gather_binding(
    binding: Any,
    *,
    kusto_query_executor: Any | None,
    include_unvalidated: bool,
) -> Any:
    from src.core.kusto_discovery import KustoDiscoveryProvider
    from src.core.kusto_hydration import KustoHydrationProvider

    base_query_loader = getattr(binding.hydration_provider, "_query_loader", load_kpi_queries)
    filtered_query_loader = kusto_query_loader_without_refresh_on_gather(base_query_loader)
    binding = replace(
        binding,
        discovery_provider=KustoDiscoveryProvider(query_loader=filtered_query_loader),
        hydration_provider=KustoHydrationProvider(
            executor=kusto_query_executor or getattr(binding.hydration_provider, "_executor", build_live_kusto_query_executor()),
            query_loader=filtered_query_loader,
        ),
    )
    if include_unvalidated:
        discovery_config: Any = binding.discovery_config
        hydration_config: Any = binding.hydration_config
        if hasattr(discovery_config, "include_unvalidated"):
            discovery_config = replace(discovery_config, include_unvalidated=True)
        if hasattr(hydration_config, "include_unvalidated"):
            hydration_config = replace(hydration_config, include_unvalidated=True)
        binding = replace(
            binding,
            discovery_config=discovery_config,
            hydration_config=hydration_config,
        )
    return binding


def kusto_query_loader_without_refresh_on_gather(
    base_query_loader: Callable[..., tuple[KustoQuery, ...]],
) -> Callable[..., tuple[KustoQuery, ...]]:
    def _wrapped(program_id: str, programs_root: Path) -> tuple[KustoQuery, ...]:
        return tuple(
            query
            for query in base_query_loader(program_id, programs_root=programs_root)
            if not query.refresh_on_gather
        )

    return _wrapped


def record_uil_kusto_query_states(
    program_id: str,
    *,
    binding: Any,
    hydration_result: Any,
    as_of: datetime,
    query_state_sink: dict[str, dict[str, Any]] | None,
    previous_query_states: dict[str, dict[str, Any]] | None,
    include_unvalidated: bool,
    record_kusto_query_state_fn: Callable[..., None],
) -> None:
    if query_state_sink is None:
        return
    query_loader = getattr(binding.hydration_provider, "_query_loader", load_kpi_queries)
    queries_by_id = {
        query.id: query
        for query in query_loader(program_id, programs_root=binding.hydration_config.programs_root)
        if query.engine == "kusto" and (include_unvalidated or query.validated)
    }
    result_sets_by_id = {
        result_set.query_id: result_set
        for result_set in getattr(hydration_result.resources, "result_sets", ())
    }
    errors_by_ref: dict[str, str] = {}
    for error in hydration_result.errors:
        if error.ref_id:
            errors_by_ref.setdefault(error.ref_id, error.message)
    for query_id, result_set in result_sets_by_id.items():
        query = queries_by_id.get(query_id)
        if query is None:
            continue
        record_kusto_query_state_fn(
            query_state_sink,
            query,
            rows=[dict(row) for row in result_set.rows],
            as_of=as_of,
            duration_ms=0,
            previous_state=(previous_query_states or {}).get(query.id),
        )
    for query_id, error_message in errors_by_ref.items():
        if query_id in result_sets_by_id:
            continue
        query = queries_by_id.get(query_id)
        if query is None:
            continue
        record_kusto_query_state_fn(
            query_state_sink,
            query,
            rows=[],
            as_of=as_of,
            duration_ms=0,
            error=error_message,
            previous_state=(previous_query_states or {}).get(query.id),
        )


def _run_signal_channel_via_uil(
    program: Program,
    as_of: datetime,
    *,
    programs_root: Path,
    binding: Any,
    integration_error_sink: list[Any] | None,
    env_flag_fn: Callable[[str], bool],
    run_channel_with_extraction_fn: Callable[..., tuple[Any | None, Any | None, Any | None]],
) -> tuple[Any | None, Any | None]:
    hydration_result, extraction_result, _ = run_channel_with_extraction_fn(
        binding,
        _build_uil_store(program.id, programs_root),
        program_id=program.id,
        since=as_of,
        verified_at=as_of,
        run_ctx=_build_uil_run_context(env_flag_fn),
        integration_error_sink=integration_error_sink,
    )
    return hydration_result, extraction_result


def _build_uil_store(program_id: str, programs_root: Path) -> ChannelRegistryStore:
    return ChannelRegistryStore(get_channel_registry_path(program_id, programs_root=programs_root), program_id)


def _build_uil_run_context(env_flag_fn: Callable[[str], bool]) -> RunContext:
    return RunContext(
        dry_run=False,
        force_discovery=env_flag_fn("VERTEX_UIL_FORCE_DISCOVERY"),
        accept_shrinkage=env_flag_fn("VERTEX_UIL_ACCEPT_SHRINKAGE"),
    )
