from __future__ import annotations

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
class KustoHydrationConfig:
    programs_root: Path = PROGRAMS_ROOT
    include_unvalidated: bool = False
    area_paths: tuple[str, ...] = ()
    date_window_days: int | None = None


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
        result_sets: list[KustoResultSet] = []
        hydrated_ref_ids: list[tuple[str, str]] = []
        failed_ref_ids: list[tuple[str, str]] = []
        errors: list[IntegrationError] = []
        api_call_count = 0
        observed_at = datetime.now(timezone.utc)

        for registration in registrations:
            if registration.ref_kind != "kusto_query":
                continue
            query = queries_by_id.get(registration.ref_id)
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
                api_call_count += 1
            except Exception as error:
                failed_ref_ids.append((registration.ref_id, registration.ref_kind))
                errors.append(
                    IntegrationError(
                        source="kusto",
                        stage="hydration",
                        retryable=True,
                        message=str(error),
                        ref_id=registration.ref_id,
                        ref_kind=registration.ref_kind,
                    )
                )
                continue
            result_sets.append(
                KustoResultSet(
                    query_id=registration.ref_id,
                    rows=tuple(_normalize_kusto_row(row) for row in rows),
                    observed_at=observed_at,
                    workstream_ids=registration.workstream_ids,
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