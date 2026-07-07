from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from src.core.config_loader import PROGRAMS_ROOT
from src.core.integration_types import (
    ChannelConfig,
    ChannelRegistration,
    DiscoveredRef,
    DiscoveryCompleteness,
    DiscoveryResult,
    ProviderCapability,
    RegistrationBinding,
    RegistrationStatus,
    ScopeStatus,
    ScopeStatusKind,
    HydrationMode,
)
from src.core.kusto_query_loader import load_kpi_queries
from src.core.models_v2 import KustoQuery, Program, Workstream


@dataclass(frozen=True, slots=True)
class KustoDiscoveryConfig:
    programs_root: Path = PROGRAMS_ROOT
    include_unvalidated: bool = False
    provider_instance_id: str = "default"
    schema_introspection_enabled: bool = False


class KustoDiscoveryProvider:
    def __init__(self, query_loader=load_kpi_queries):
        self._query_loader = query_loader

    @classmethod
    def from_program(
        cls,
        program: Program,
        channel_config: ChannelConfig,
        workstreams: tuple[Workstream, ...],
        *,
        programs_root: Path = PROGRAMS_ROOT,
    ) -> tuple["KustoDiscoveryProvider", KustoDiscoveryConfig]:
        del workstreams
        if program.kusto is None or not program.kusto.enabled:
            raise ValueError(f"Program '{program.id}' has no Kusto config")
        return cls(), KustoDiscoveryConfig(
            programs_root=programs_root,
            include_unvalidated=bool((channel_config.extra or {}).get("include_unvalidated", False)),
            provider_instance_id=str((channel_config.extra or {}).get("instance_id") or "default"),
            schema_introspection_enabled=bool((channel_config.extra or {}).get("schema_introspection_enabled", False)),
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

    def discover(
        self,
        program_id: str,
        config: KustoDiscoveryConfig,
        existing: tuple[ChannelRegistration, ...],
        run_ctx: object = None,
    ) -> DiscoveryResult:
        del existing, run_ctx
        computed_at = datetime.now(timezone.utc)
        queries = tuple(
            query
            for query in self._query_loader(program_id, programs_root=config.programs_root)
            if query.engine == "kusto" and (config.include_unvalidated or query.validated)
        )
        discovered_refs = tuple(
            _query_to_discovered_ref(
                query,
                program_id=program_id,
                provider_instance_id=config.provider_instance_id,
                computed_at=computed_at,
            )
            for query in queries
        )
        return DiscoveryResult(
            channel="kusto",
            program_id=program_id,
            discovered_refs=discovered_refs,
            completeness=DiscoveryCompleteness.FULL,
            scope_statuses={
                "query_catalog": ScopeStatus(
                    scope_id="query_catalog",
                    status=ScopeStatusKind.SUCCESS,
                    completeness=DiscoveryCompleteness.FULL,
                    item_count=len(discovered_refs),
                )
            },
            scope_state_updates={},
            errors=(),
            computed_at=computed_at,
            provider_instance_id=config.provider_instance_id,
        )


def _query_to_discovered_ref(
    query: KustoQuery,
    *,
    program_id: str,
    provider_instance_id: str,
    computed_at: datetime,
) -> DiscoveredRef:
    registration = ChannelRegistration(
        channel="kusto",
        program_id=program_id,
        provider_instance_id=provider_instance_id,
        ref_id=query.id,
        ref_kind="kusto_query",
        status=RegistrationStatus.ACTIVE,
        first_discovered_at=computed_at,
        last_seen_at=computed_at,
        confidence=1.0 if query.validated else 0.75,
        confidence_source="manual_config",
        ref_title=query.label or query.section or query.id,
        metadata={
            "cluster": query.cluster,
            "database": query.database,
            "render_as": query.render_as,
            "engine": query.engine,
        },
    )
    bindings = tuple(
        RegistrationBinding(
            workstream_id=workstream_id,
            scope_id=query.id,
            source_type="kusto_query",
            confidence=registration.confidence,
            confidence_source="manual_config",
        )
        for workstream_id in query.workstream_ids
    ) or (
        RegistrationBinding(
            workstream_id=None,
            scope_id=query.id,
            source_type="kusto_query",
            confidence=registration.confidence,
            confidence_source="manual_config",
        ),
    )
    return DiscoveredRef(registration=registration, bindings=bindings)
