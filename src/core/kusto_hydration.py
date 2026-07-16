from __future__ import annotations

import concurrent.futures
import operator as _operator
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.core.config_loader import PROGRAMS_ROOT
from src.core.integration_types import (
    ChannelConfig,
    ChannelRegistration,
    DiscoveryCompleteness,
    HydrationMode,
    HydrationResult,
    IntegrationError,
    KustoHydrationOutput,
    KustoResultSet,
    ProviderCapability,
)
from src.core.kusto_client import build_live_kusto_query_executor
from src.core.kusto_query_loader import load_kpi_queries
from src.core.kusto_templates import KustoTemplateContext, render_kusto_query
from src.core.models_v2 import KustoQuery, Program, Workstream


@dataclass(frozen=True, slots=True)
class _QueryExecution:
    rows: list[dict[str, Any]] | None
    error: str | None
    timed_out: bool = False


@dataclass(frozen=True, slots=True)
class KustoHydrationConfig:
    programs_root: Path = PROGRAMS_ROOT
    include_unvalidated: bool = False
    area_paths: tuple[str, ...] = ()
    date_window_days: int | None = None
    # ADF-W1.6 (Section 8.5.3): bounded query concurrency; 1 = sequential
    # (unchanged pre-ADF-W1.6 behavior). Only raised once Phase-0/ADF-W0.6
    # ratifies parallelism from the benchmark artifact.
    max_concurrency: int = 1
    per_query_timeout_seconds: int = 60


class KustoHydrationProvider:
    def __init__(self, executor: Callable[[KustoQuery], list[dict[str, Any]]] | None = None, query_loader=load_kpi_queries):
        self._executor = executor or build_live_kusto_query_executor()
        self._query_loader = query_loader

    @classmethod
    def from_program(
        cls,
        program: Program,
        channel_config: ChannelConfig,
        workstreams: tuple[Workstream, ...],
        *,
        programs_root: Path = PROGRAMS_ROOT,
    ) -> tuple["KustoHydrationProvider", KustoHydrationConfig]:
        del workstreams
        if program.kusto is None or not program.kusto.enabled:
            raise ValueError(f"Program '{program.id}' has no Kusto config")
        return cls(), KustoHydrationConfig(
            programs_root=programs_root,
            include_unvalidated=bool((channel_config.extra or {}).get("include_unvalidated", False)),
            area_paths=program.ado.area_paths if program.ado is not None else (),
            date_window_days=program.ado.date_window_days if program.ado is not None else None,
            max_concurrency=max(1, program.kusto.max_concurrency),
        )

    @property
    def channel(self) -> str:
        return "kusto"

    @property
    def capability(self) -> ProviderCapability:
        return ProviderCapability(
            channel="kusto",
            discovery_modes=(DiscoveryCompleteness.FULL,),
            hydration_modes=(HydrationMode.FULL,),
            supports_since=False,
            max_batch_size=100,
            rate_limit_rpm=None,
            retry_max_attempts=2,
            retry_backoff_seconds=0.5,
            privacy_class="internal_content",
            timeout_seconds=60,
        )

    def hydrate(
        self,
        registrations: tuple[ChannelRegistration, ...],
        since: datetime,
        program_id: str,
        config: KustoHydrationConfig,
        mode: HydrationMode = HydrationMode.FULL,
        run_ctx: object = None,
    ) -> HydrationResult[KustoHydrationOutput]:
        del since, mode, run_ctx
        queries_by_id = {
            query.id: query
            for query in self._query_loader(program_id, programs_root=config.programs_root)
            if query.engine == "kusto" and (config.include_unvalidated or query.validated)
        }

        # ADF-W1.6 (Section 8.5.3): the work-item order is the sole source of
        # deterministic output ordering (by query_id / registration order),
        # honored identically whether execution below is sequential or
        # bounded-parallel.
        work_items: list[tuple[ChannelRegistration, KustoQuery | None]] = [
            (registration, queries_by_id.get(registration.ref_id))
            for registration in registrations
            if registration.ref_kind == "kusto_query"
        ]
        runnable = [(registration, query) for registration, query in work_items if query is not None]

        if config.max_concurrency <= 1:
            executions = self._execute_sequential(runnable, program_id, config)
        else:
            executions = self._execute_parallel(runnable, program_id, config)

        result_sets: list[KustoResultSet] = []
        hydrated_ref_ids: list[tuple[str, str]] = []
        failed_ref_ids: list[tuple[str, str]] = []
        errors: list[IntegrationError] = []
        api_call_count = 0
        observed_at = datetime.now(timezone.utc)

        for registration, query in work_items:
            if query is None:
                failed_ref_ids.append((registration.ref_id, registration.ref_kind))
                errors.append(
                    IntegrationError(
                        source="kusto",
                        stage="hydration",
                        retryable=False,
                        message=f"Unknown Kusto query id '{registration.ref_id}'",
                        ref_id=registration.ref_id,
                        ref_kind=registration.ref_kind,
                    )
                )
                continue
            execution = executions[registration.ref_id]
            if execution.error is not None:
                failed_ref_ids.append((registration.ref_id, registration.ref_kind))
                errors.append(
                    IntegrationError(
                        source="kusto",
                        stage="hydration",
                        retryable=True,
                        message=execution.error,
                        ref_id=registration.ref_id,
                        ref_kind=registration.ref_kind,
                    )
                )
                continue
            api_call_count += 1
            normalized_rows = tuple(_normalize_kusto_row(row) for row in execution.rows or ())
            observed_value = _extract_observed_value(normalized_rows, query.result_column)
            result_sets.append(
                KustoResultSet(
                    query_id=registration.ref_id,
                    rows=normalized_rows,
                    observed_at=observed_at,
                    workstream_ids=registration.workstream_ids,
                    # ADF-W2.3 (Section 8.5.1): populated only when the query
                    # declares semantic config (metric_id/result_column/...);
                    # a query without it keeps every field at its None default,
                    # unchanged from before this item.
                    metric_id=query.metric_id,
                    result_column=query.result_column,
                    unit=query.unit,
                    slo_target=query.slo_target,
                    comparison=query.comparison,
                    observed_value=observed_value,
                    is_breach=_compute_is_breach(observed_value, query.slo_target, query.comparison),
                    row_count=len(normalized_rows),
                )
            )
            hydrated_ref_ids.append((registration.ref_id, registration.ref_kind))

        return HydrationResult(
            channel="kusto",
            resources=KustoHydrationOutput(result_sets=tuple(result_sets)),
            api_call_count=api_call_count,
            errors=tuple(errors),
            hydrated_ref_ids=tuple(hydrated_ref_ids),
            failed_ref_ids=tuple(failed_ref_ids),
        )

    def _execute_sequential(
        self,
        runnable: list[tuple[ChannelRegistration, KustoQuery]],
        program_id: str,
        config: KustoHydrationConfig,
    ) -> dict[str, _QueryExecution]:
        """Unchanged pre-ADF-W1.6 behavior: one query at a time, in order."""
        executions: dict[str, _QueryExecution] = {}
        for registration, query in runnable:
            executions[registration.ref_id] = self._run_one(registration, query, program_id, config)
        return executions

    def _execute_parallel(
        self,
        runnable: list[tuple[ChannelRegistration, KustoQuery]],
        program_id: str,
        config: KustoHydrationConfig,
    ) -> dict[str, _QueryExecution]:
        """ADF-W1.6 (Section 8.5.3): bounded concurrency, per-query timeout,
        no cross-cancellation -- one query's failure or timeout never
        affects any other query's independent execution or result.

        Each future is awaited individually with its own
        ``per_query_timeout_seconds`` budget (not a shared/global timeout via
        ``as_completed`` -- that would only ever observe already-finished
        futures and enforce nothing). All queries still run concurrently in
        the pool regardless of the order their results are collected in, so
        one slow query does not add to another's wait. On timeout the
        executor is shut down without waiting (matching
        ``channel_execution_policy.run_under_channel_budget``'s documented
        thread-abandonment trade-off) so a hung query cannot block the
        caller past its own budget.
        """
        executions: dict[str, _QueryExecution] = {}
        if not runnable:
            return executions
        max_workers = min(config.max_concurrency, len(runnable))
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="kusto-query")
        future_to_ref_id = {
            executor.submit(self._run_one, registration, query, program_id, config): registration.ref_id
            for registration, query in runnable
        }
        try:
            for future, ref_id in future_to_ref_id.items():
                try:
                    executions[ref_id] = future.result(timeout=config.per_query_timeout_seconds)
                except concurrent.futures.TimeoutError:
                    executions[ref_id] = _QueryExecution(
                        rows=None,
                        error=f"Kusto query '{ref_id}' exceeded its {config.per_query_timeout_seconds}s per-query budget.",
                        timed_out=True,
                    )
                except Exception as error:  # pragma: no cover - _run_one already catches provider errors
                    executions[ref_id] = _QueryExecution(rows=None, error=str(error))
        finally:
            executor.shutdown(wait=False)
        return executions

    def _run_one(
        self,
        registration: ChannelRegistration,
        query: KustoQuery,
        program_id: str,
        config: KustoHydrationConfig,
    ) -> _QueryExecution:
        rendered_query = render_kusto_query(
            query,
            context=KustoTemplateContext(
                program_id=program_id,
                area_paths=config.area_paths,
                date_window_days=config.date_window_days,
            ),
        )
        try:
            rows = self._executor(rendered_query)
        except Exception as error:
            return _QueryExecution(rows=None, error=str(error))
        return _QueryExecution(rows=rows, error=None)


def _normalize_kusto_row(row: dict[str, Any]) -> dict[str, str | int | float | bool | None]:
    normalized: dict[str, str | int | float | bool | None] = {}
    for key, value in row.items():
        if not isinstance(key, str) or not key.strip():
            continue
        normalized[key] = _normalize_kusto_value(value)
    return normalized


def _normalize_kusto_value(value: Any) -> str | int | float | bool | None:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat() if value.tzinfo is not None else value.replace(tzinfo=timezone.utc).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


#: ADF-W2.3 (Section 8.5.1): the same five comparators the spec's SLO
#: examples use. Unrecognized comparison strings degrade to "no verdict"
#: (None) rather than raising -- a query author typo must not crash gather.
_COMPARISON_OPERATORS: dict[str, Callable[[float, float], bool]] = {
    ">=": _operator.ge,
    "<=": _operator.le,
    "==": _operator.eq,
    ">": _operator.gt,
    "<": _operator.lt,
}


def _extract_observed_value(
    rows: tuple[dict[str, str | int | float | bool | None], ...],
    result_column: str | None,
) -> float | None:
    """Section 8.5.1: the scalar the semantic signal is judged on. Kusto
    scalar/SLO queries return exactly one row; only the first row is
    consulted. Returns None (not 0.0) when the column is absent/non-numeric
    so callers can distinguish "no semantic value" from "value is zero"."""
    if not result_column or not rows:
        return None
    raw = rows[0].get(result_column)
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _compute_is_breach(observed_value: float | None, slo_target: float | None, comparison: str | None) -> bool | None:
    """Section 8.5.1/8.5.2: None when there is no SLO configured or no
    observed value to judge (an unconfigured query stays silent, not
    falsely "OK"); True/False only when a real verdict can be reached."""
    if observed_value is None or slo_target is None or comparison is None:
        return None
    comparator = _COMPARISON_OPERATORS.get(comparison)
    if comparator is None:
        return None
    return not comparator(observed_value, slo_target)