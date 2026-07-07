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
    HydrationMode,
    ProviderCapability,
    RegistrationBinding,
    RegistrationStatus,
    RunContext,
    ScopeStatus,
    ScopeStatusKind,
)
from src.core.models_v2 import Program, Workstream


_STATIC_SCOPE_ID = "static_config"
_STATIC_CONFIDENCE = 1.0


@dataclass(frozen=True, slots=True)
class EmailDiscoveryConfig:
    workstreams: tuple[Workstream, ...]
    provider_instance_id: str = "default"


class EmailDiscoveryProvider:
    @classmethod
    def from_program(
        cls,
        program: Program,
        channel_config: ChannelConfig,
        workstreams: tuple[Workstream, ...],
        *,
        programs_root: Path = PROGRAMS_ROOT,
    ) -> tuple["EmailDiscoveryProvider", EmailDiscoveryConfig]:
        del program, programs_root
        return cls(), EmailDiscoveryConfig(
            workstreams=workstreams,
            provider_instance_id=str((channel_config.extra or {}).get("instance_id") or "default"),
        )

    @property
    def channel(self) -> str:
        return "email"

    @property
    def capability(self) -> ProviderCapability:
        return ProviderCapability(
            channel="email",
            discovery_modes=(DiscoveryCompleteness.FULL,),
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
        config: EmailDiscoveryConfig,
        existing_registrations: tuple[ChannelRegistration, ...],
        *,
        run_ctx: RunContext,
    ) -> DiscoveryResult:
        del existing_registrations, run_ctx
        now = datetime.now(timezone.utc)
        refs: list[DiscoveredRef] = []
        for workstream in config.workstreams:
            signal_sources = workstream.signal_sources
            if signal_sources is None:
                continue
            for email_thread in signal_sources.email_threads:
                refs.append(
                    DiscoveredRef(
                        registration=ChannelRegistration(
                            channel="email",
                            program_id=program_id,
                            ref_id=email_thread.thread_id,
                            ref_kind="email_thread",
                            status=RegistrationStatus.ACTIVE,
                            first_discovered_at=now,
                            last_seen_at=now,
                            confidence=_STATIC_CONFIDENCE,
                            confidence_source="static_config",
                            provider_instance_id=config.provider_instance_id,
                            ref_title=email_thread.display_name,
                            metadata={
                                "display_name": email_thread.display_name,
                                "thread_id": email_thread.thread_id,
                            },
                            work_item_ids=email_thread.work_item_ids,
                        ),
                        bindings=(
                            RegistrationBinding(
                                workstream_id=workstream.id,
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
        return DiscoveryResult(
            channel="email",
            program_id=program_id,
            discovered_refs=tuple(refs),
            completeness=DiscoveryCompleteness.FULL,
            scope_statuses={
                _STATIC_SCOPE_ID: ScopeStatus(
                    scope_id=_STATIC_SCOPE_ID,
                    status=ScopeStatusKind.SUCCESS,
                    completeness=DiscoveryCompleteness.FULL,
                    item_count=len(refs),
                )
            },
            scope_state_updates={},
            errors=(),
            computed_at=now,
            provider_instance_id=config.provider_instance_id,
        )
