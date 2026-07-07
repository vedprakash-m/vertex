from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.core.config_loader import PROGRAMS_ROOT
from src.core.exceptions import QueryError
from src.core.integration_types import (
    ChannelConfig,
    ChannelRegistration,
    DiscoveredRef,
    DiscoveryCompleteness,
    DiscoveryResult,
    HydrationMode,
    IntegrationError,
    ProviderCapability,
    RegistrationBinding,
    RegistrationStatus,
    RunContext,
    ScopeStatus,
    ScopeStatusKind,
    ScopeState,
)
from src.core.models_v2 import Program, Workstream
from src.m365.agency_bridge import AgencyBridge
from src.m365.graph_calendar_client import GraphCalendarClient
from src.m365.series_id_resolver import CalendarSeriesIdResolver, SeriesIdResolver
from src.m365.teams_reader import TeamsReader


_LOG = logging.getLogger(__name__)

_STATIC_SCOPE_ID = "static_config"
_WORKIQ_SCOPE_ID = "workiq_search"
_STATIC_CONFIDENCE = 1.0
_WORKIQ_CONFIDENCE = 0.6
_WORKIQ_SEARCH_LIMIT = 50


@dataclass(frozen=True, slots=True)
class TeamsDiscoveryConfig:
    workstreams: tuple[Workstream, ...]
    workiq_keywords: tuple[str, ...]
    provider_instance_id: str = "default"
    workiq_enabled: bool = True
    # Per-program match aliases sourced from workstreams.yaml — kept out of core
    match_aliases: tuple[Any, ...] = ()


class TeamsDiscoveryProvider:
    def __init__(
        self,
        calendar_client: GraphCalendarClient,
        teams_reader: TeamsReader,
        *,
        series_id_resolver: SeriesIdResolver | None = None,
    ) -> None:
        self._calendar_client = calendar_client
        self._teams_reader = teams_reader
        self._series_id_resolver = series_id_resolver

    @classmethod
    def from_program(
        cls,
        program: Program,
        channel_config: ChannelConfig,
        workstreams: tuple[Workstream, ...],
        *,
        programs_root: Path = PROGRAMS_ROOT,
    ) -> tuple["TeamsDiscoveryProvider", TeamsDiscoveryConfig]:
        del programs_root
        bridge = AgencyBridge()
        calendar_client = GraphCalendarClient(bridge)
        teams_reader = TeamsReader(bridge)
        series_id_resolver: SeriesIdResolver | None = CalendarSeriesIdResolver(bridge)
        workiq_keywords: list[str] = []
        for ws in workstreams:
            if ws.signal_sources is not None:
                workiq_keywords.extend(ws.signal_sources.workiq_keywords)
        config = TeamsDiscoveryConfig(
            workstreams=workstreams,
            workiq_keywords=tuple(dict.fromkeys(workiq_keywords)),
            provider_instance_id=str((channel_config.extra or {}).get("instance_id") or "default"),
            workiq_enabled=bool((channel_config.extra or {}).get("workiq_enabled", True)),
        )
        return cls(calendar_client, teams_reader, series_id_resolver=series_id_resolver), config

    @property
    def channel(self) -> str:
        return "teams"

    @property
    def capability(self) -> ProviderCapability:
        return ProviderCapability(
            channel="teams",
            discovery_modes=(DiscoveryCompleteness.FULL, DiscoveryCompleteness.INCREMENTAL),
            hydration_modes=(HydrationMode.FULL,),
            supports_since=False,
            max_batch_size=50,
            rate_limit_rpm=60,
            retry_max_attempts=3,
            retry_backoff_seconds=2.0,
            privacy_class="internal_content",
            timeout_seconds=30,
        )

    def discover(
        self,
        program_id: str,
        config: TeamsDiscoveryConfig,
        existing_registrations: tuple[ChannelRegistration, ...],
        *,
        run_ctx: RunContext,
    ) -> DiscoveryResult:
        errors: list[IntegrationError] = []
        scope_statuses: dict[str, ScopeStatus] = {}
        scope_state_updates: dict[str, ScopeState] = {}
        discovered_refs: list[DiscoveredRef] = []

        # --- Static config scope (FULL completeness) ---
        static_refs, static_status = self._discover_static(program_id, config)
        discovered_refs.extend(static_refs)
        scope_statuses[_STATIC_SCOPE_ID] = static_status

        # --- WorkIQ search scope (INCREMENTAL completeness) ---
        workiq_status: ScopeStatus
        if config.workiq_enabled and config.workiq_keywords:
            workiq_refs, workiq_status, workiq_errors = self._discover_workiq(
                program_id, config, existing_registrations
            )
            discovered_refs.extend(workiq_refs)
            errors.extend(workiq_errors)
        else:
            workiq_status = ScopeStatus(
                scope_id=_WORKIQ_SCOPE_ID,
                status=ScopeStatusKind.SUCCESS,
                completeness=DiscoveryCompleteness.INCREMENTAL,
                item_count=0,
            )
        scope_statuses[_WORKIQ_SCOPE_ID] = workiq_status

        # Overall completeness: minimum safety level across scopes
        # Static is FULL, WorkIQ is INCREMENTAL → overall INCREMENTAL
        overall_completeness = DiscoveryCompleteness.INCREMENTAL

        return DiscoveryResult(
            channel="teams",
            program_id=program_id,
            discovered_refs=tuple(discovered_refs),
            completeness=overall_completeness,
            scope_statuses=scope_statuses,
            scope_state_updates=scope_state_updates,
            errors=tuple(errors),
            computed_at=datetime.now(timezone.utc),
            provider_instance_id=config.provider_instance_id,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _discover_static(
        self,
        program_id: str,
        config: TeamsDiscoveryConfig,
    ) -> tuple[list[DiscoveredRef], ScopeStatus]:
        refs: list[DiscoveredRef] = []
        now = datetime.now(timezone.utc)

        for ws in config.workstreams:
            if ws.signal_sources is None:
                continue
            for meeting_series in ws.signal_sources.teams_meeting_series:
                ref_id = meeting_series.series_id
                confidence = _STATIC_CONFIDENCE
                confidence_source = "static_config"
                pm_confirmed = True

                if ref_id is None:
                    # Auto-discover the series_id from the calendar if a resolver
                    # is available.  The resolver returns None when confidence is
                    # too low or the result is ambiguous — in that case we skip
                    # this entry (same as the previous hard-skip behaviour) so
                    # that downstream callers are never given a speculative ID.
                    resolved = self._try_resolve_series_id(
                        meeting_series.display_name,
                        topics=tuple(ws.signal_sources.workiq_keywords) if ws.signal_sources else (),
                        match_aliases=tuple(config.match_aliases),
                    )
                    if resolved is None:
                        _LOG.debug(
                            "Skipping meeting series %r for workstream %r: "
                            "no series_id configured and auto-resolution returned no result",
                            meeting_series.display_name,
                            ws.id,
                        )
                        continue
                    ref_id, resolved_confidence = resolved
                    confidence = resolved_confidence
                    confidence_source = "auto_resolved"
                    pm_confirmed = False  # operator confirmation still recommended

                refs.append(
                    DiscoveredRef(
                        registration=ChannelRegistration(
                            channel="teams",
                            program_id=program_id,
                            ref_id=ref_id,
                            ref_kind="meeting_series",
                            status=RegistrationStatus.ACTIVE,
                            first_discovered_at=now,
                            last_seen_at=now,
                            confidence=confidence,
                            confidence_source=confidence_source,
                            provider_instance_id=config.provider_instance_id,
                            ref_title=meeting_series.display_name,
                            metadata={
                                "display_name": meeting_series.display_name,
                                "auto_resolved": meeting_series.series_id is None,
                            },
                            work_item_ids=meeting_series.work_item_ids,
                        ),
                        bindings=(
                            RegistrationBinding(
                                workstream_id=ws.id,
                                scope_id=_STATIC_SCOPE_ID,
                                source_type="static_config",
                                confidence=confidence,
                                confidence_source=confidence_source,
                                pm_confirmed=pm_confirmed,
                                promoted=False,
                            ),
                        ),
                    )
                )
            for teams_chat in ws.signal_sources.teams_chats:
                if teams_chat.thread_id is None:
                    continue
                ref_id = teams_chat.thread_id
                refs.append(
                    DiscoveredRef(
                        registration=ChannelRegistration(
                            channel="teams",
                            program_id=program_id,
                            ref_id=ref_id,
                            ref_kind="teams_chat",
                            status=RegistrationStatus.ACTIVE,
                            first_discovered_at=now,
                            last_seen_at=now,
                            confidence=_STATIC_CONFIDENCE,
                            confidence_source="static_config",
                            provider_instance_id=config.provider_instance_id,
                            ref_title=teams_chat.display_name,
                            metadata={"display_name": teams_chat.display_name, "thread_id": teams_chat.thread_id},
                            work_item_ids=teams_chat.work_item_ids,
                        ),
                        bindings=(
                            RegistrationBinding(
                                workstream_id=ws.id,
                                scope_id=_STATIC_SCOPE_ID,
                                source_type="static_config",
                                confidence=_STATIC_CONFIDENCE,
                                confidence_source="static_config",
                                pm_confirmed=True,
                                promoted=False,
                            ),
                        ),
                    )
                )

        status = ScopeStatus(
            scope_id=_STATIC_SCOPE_ID,
            status=ScopeStatusKind.SUCCESS,
            completeness=DiscoveryCompleteness.FULL,
            item_count=len(refs),
        )
        return refs, status

    def _discover_workiq(
        self,
        program_id: str,
        config: TeamsDiscoveryConfig,
        existing_registrations: tuple[ChannelRegistration, ...],
    ) -> tuple[list[DiscoveredRef], ScopeStatus, list[IntegrationError]]:
        existing_ids = {r.ref_id for r in existing_registrations}
        refs: list[DiscoveredRef] = []
        errors: list[IntegrationError] = []
        now = datetime.now(timezone.utc)
        item_count = 0

        try:
            for keyword in config.workiq_keywords:
                page = self._teams_reader.search_messages(
                    channel=keyword,
                    query=keyword,
                    limit=_WORKIQ_SEARCH_LIMIT,
                )
                for record in page.records:
                    if not record.source_id:
                        continue
                    thread_id = record.source_id
                    if thread_id in existing_ids:
                        continue  # already in registry; don't re-add
                    refs.append(
                        DiscoveredRef(
                            registration=ChannelRegistration(
                            channel="teams",
                            program_id=program_id,
                            ref_id=thread_id,
                            ref_kind="teams_chat",
                            status=RegistrationStatus.ACTIVE,
                            first_discovered_at=now,
                            last_seen_at=now,
                            confidence=_WORKIQ_CONFIDENCE,
                            confidence_source="workiq_search",
                            provider_instance_id=config.provider_instance_id,
                            ref_title=record.preview[:80] if record.preview else None,
                            metadata={"channel": record.channel or "", "sender": record.sender or ""},
                        ),
                            bindings=(),
                        )
                    )
                    item_count += 1
        except Exception as exc:
            errors.append(
                IntegrationError(
                    source="teams",
                    stage="discovery",
                    message=f"WorkIQ Teams search failed: {exc}",
                    retryable=True,
                )
            )
            return refs, ScopeStatus(
                scope_id=_WORKIQ_SCOPE_ID,
                status=ScopeStatusKind.ERROR,
                completeness=DiscoveryCompleteness.PARTIAL,
                item_count=item_count,
                error_message=str(exc),
            ), errors

        status = ScopeStatus(
            scope_id=_WORKIQ_SCOPE_ID,
            status=ScopeStatusKind.SUCCESS,
            completeness=DiscoveryCompleteness.INCREMENTAL,
            item_count=item_count,
        )
        return refs, status, errors

    def _try_resolve_series_id(
        self,
        display_name: str,
        *,
        topics: tuple[str, ...],
        match_aliases: tuple[Any, ...],
    ) -> tuple[str, float] | None:
        """Attempt to auto-resolve a series_id via the injected resolver.

        Returns ``(series_id, confidence)`` on success, or ``None`` when no
        resolver is available or when the resolver returns no unambiguous result.
        Swallows all exceptions so a transient WorkIQ failure never breaks the
        overall discover pass.
        """
        if self._series_id_resolver is None:
            return None
        try:
            return self._series_id_resolver(
                display_name,
                topics=topics,
                match_aliases=match_aliases,
            )
        except Exception as exc:
            _LOG.warning(
                "Unexpected error from series_id resolver for %r: %s",
                display_name,
                exc,
            )
            return None
