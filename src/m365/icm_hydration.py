from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.core.config_loader import PROGRAMS_ROOT
from src.core.exceptions import AuthError, QueryError
from src.core.integration_types import (
    ChannelConfig,
    ChannelRegistration,
    DiscoveryCompleteness,
    HydrationMode,
    HydrationResult,
    IcMHydrationOutput,
    IncidentState,
    IntegrationError,
    ProviderCapability,
    RunContext,
)
from src.core.models_v2 import Program, Workstream


@dataclass(frozen=True, slots=True)
class IcMHydrationConfig:
    provider_instance_id: str = "default"


class IcMHydrationProvider:
    @classmethod
    def from_program(
        cls,
        program: Program,
        channel_config: ChannelConfig,
        workstreams: tuple[Workstream, ...],
        *,
        programs_root: Path = PROGRAMS_ROOT,
    ) -> tuple["IcMHydrationProvider", IcMHydrationConfig]:
        del program, workstreams, programs_root
        extra = channel_config.extra or {}
        config = IcMHydrationConfig(
            provider_instance_id=str(extra.get("instance_id") or "default"),
        )
        return cls(), config

    @property
    def channel(self) -> str:
        return "icm"

    @property
    def capability(self) -> ProviderCapability:
        return ProviderCapability(
            channel="icm",
            discovery_modes=(DiscoveryCompleteness.FULL, DiscoveryCompleteness.INCREMENTAL),
            hydration_modes=(HydrationMode.FULL,),
            supports_since=True,
            max_batch_size=200,
            rate_limit_rpm=30,
            retry_max_attempts=3,
            retry_backoff_seconds=5.0,
            privacy_class="internal_content",
            timeout_seconds=30,
        )

    def hydrate(
        self,
        registrations: tuple[ChannelRegistration, ...],
        since: datetime,
        program_id: str,
        config: IcMHydrationConfig,
        *,
        mode: HydrationMode = HydrationMode.FULL,
        run_ctx: RunContext,
    ) -> HydrationResult[IcMHydrationOutput]:
        del mode  # IcM has only FULL mode
        incident_regs = [r for r in registrations if r.ref_kind == "incident"]
        # Skip icm_team refs — they are non-hydratable catalog entries
        if not incident_regs:
            return HydrationResult(
                channel="icm",
                resources=IcMHydrationOutput(incident_states=()),
                api_call_count=0,
                errors=(),
                hydrated_ref_ids=(),
                failed_ref_ids=(),
            )

        from src.m365.icm_client import IcmClient

        try:
            client = IcmClient()
        except AuthError as exc:
            return HydrationResult(
                channel="icm",
                resources=IcMHydrationOutput(incident_states=()),
                api_call_count=0,
                errors=(IntegrationError(source="icm", stage="hydration", message=str(exc), retryable=False),),
                hydrated_ref_ids=(),
                failed_ref_ids=tuple((r.ref_id, r.ref_kind) for r in incident_regs),
            )

        incidents: list[IncidentState] = []
        errors: list[IntegrationError] = []
        hydrated_ref_ids: list[tuple[str, str]] = []
        failed_ref_ids: list[tuple[str, str]] = []
        api_call_count = 0

        for reg in incident_regs:
            try:
                payload = client.list_incidents(params={"id": reg.ref_id, "$top": 1})
                raw_list = payload.get("items") or payload.get("value") or []
                api_call_count += 1
                if raw_list and isinstance(raw_list[0], dict):
                    state = _parse_incident(raw_list[0], reg.workstream_ids)
                else:
                    # Fallback: build minimal state from registration metadata
                    state = _incident_from_registration(reg)
                incidents.append(state)
                hydrated_ref_ids.append((reg.ref_id, reg.ref_kind))
            except (QueryError, RuntimeError) as exc:
                errors.append(IntegrationError(
                    source="icm",
                    stage="hydration",
                    message=f"Failed to hydrate incident {reg.ref_id}: {exc}",
                    retryable=True,
                ))
                failed_ref_ids.append((reg.ref_id, reg.ref_kind))

        return HydrationResult(
            channel="icm",
            resources=IcMHydrationOutput(incident_states=tuple(incidents)),
            api_call_count=api_call_count,
            errors=tuple(errors),
            hydrated_ref_ids=tuple(hydrated_ref_ids),
            failed_ref_ids=tuple(failed_ref_ids),
        )


def _parse_incident(raw: dict[str, Any], workstream_ids: tuple[str, ...]) -> IncidentState:
    return IncidentState(
        incident_id=str(raw.get("id") or raw.get("IncidentId") or ""),
        title=str(raw.get("title") or raw.get("Title") or "") or None,
        severity=_safe_int(raw.get("severity") or raw.get("Severity")),
        status=str(raw.get("status") or raw.get("Status") or ""),
        owning_team=str(raw.get("owningTeamName") or raw.get("OwningTeamName") or "") or None,
        updated_at=_parse_dt(raw.get("modifiedAt") or raw.get("ModifiedDate")) or datetime.now(timezone.utc),
        workstream_ids=workstream_ids,
    )


def _incident_from_registration(reg: ChannelRegistration) -> IncidentState:
    meta = reg.metadata or {}
    return IncidentState(
        incident_id=reg.ref_id,
        title=reg.ref_title,
        severity=_safe_int(meta.get("severity")),
        status="",
        owning_team=str(meta.get("owning_team") or "") or None,
        updated_at=datetime.now(timezone.utc),
        workstream_ids=reg.workstream_ids,
    )


def _safe_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
