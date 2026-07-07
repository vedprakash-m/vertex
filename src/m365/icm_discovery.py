from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from src.core.config_loader import PROGRAMS_ROOT
from src.core.exceptions import AuthError, QueryError
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


_TEAM_MAPPING_SCOPE_ID = "team_mappings"
_INCIDENT_SCOPE_ID = "active_incidents"
_DEFAULT_SEVERITY_FILTER = (0, 1, 2)  # Sev 0/1/2 only
_INCIDENT_PAGE_SIZE = 200


@dataclass(frozen=True, slots=True)
class IcMDiscoveryConfig:
    owning_teams: tuple[str, ...]
    severity_filter: tuple[int, ...] = _DEFAULT_SEVERITY_FILTER
    provider_instance_id: str = "default"


class IcMDiscoveryProvider:
    @classmethod
    def from_program(
        cls,
        program: Program,
        channel_config: ChannelConfig,
        workstreams: tuple[Workstream, ...],
        *,
        programs_root: Path = PROGRAMS_ROOT,
    ) -> tuple["IcMDiscoveryProvider", IcMDiscoveryConfig]:
        del programs_root
        # Collect owning teams from program config (channels.icm.extra.owning_teams)
        extra = channel_config.extra or {}
        raw_teams = extra.get("owning_teams") or []
        if isinstance(raw_teams, str):
            raw_teams = [t.strip() for t in raw_teams.split(",") if t.strip()]
        owning_teams = tuple(str(t) for t in raw_teams if t)  # type: ignore[union-attr]
        sev_raw = extra.get("severity_filter")
        severity_filter: tuple[int, ...]
        if sev_raw and isinstance(sev_raw, (list, tuple)):
            severity_filter = tuple(int(s) for s in sev_raw)  # type: ignore[union-attr,arg-type]
        else:
            severity_filter = _DEFAULT_SEVERITY_FILTER
        config = IcMDiscoveryConfig(
            owning_teams=owning_teams,
            severity_filter=severity_filter,
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

    def discover(
        self,
        program_id: str,
        config: IcMDiscoveryConfig,
        existing_registrations: tuple[ChannelRegistration, ...],
        *,
        run_ctx: RunContext,
    ) -> DiscoveryResult:
        errors: list[IntegrationError] = []
        scope_statuses: dict[str, ScopeStatus] = {}
        scope_state_updates: dict[str, ScopeState] = {}
        discovered_refs: list[DiscoveredRef] = []
        now = datetime.now(timezone.utc)

        # --- Team mapping scope (FULL — static from config) ---
        team_refs = _build_team_mapping_refs(config, program_id, now)
        discovered_refs.extend(team_refs)
        scope_statuses[_TEAM_MAPPING_SCOPE_ID] = ScopeStatus(
            scope_id=_TEAM_MAPPING_SCOPE_ID,
            status=ScopeStatusKind.SUCCESS,
            completeness=DiscoveryCompleteness.FULL,
            item_count=len(team_refs),
        )

        # --- Active incidents scope (INCREMENTAL — requires live IcM API) ---
        incident_refs, incident_status, incident_errors = self._discover_incidents(
            program_id, config, existing_registrations, now
        )
        discovered_refs.extend(incident_refs)
        errors.extend(incident_errors)
        scope_statuses[_INCIDENT_SCOPE_ID] = incident_status

        overall_completeness = DiscoveryCompleteness.INCREMENTAL

        return DiscoveryResult(
            channel="icm",
            program_id=program_id,
            discovered_refs=tuple(discovered_refs),
            completeness=overall_completeness,
            scope_statuses=scope_statuses,
            scope_state_updates=scope_state_updates,
            errors=tuple(errors),
            computed_at=now,
            provider_instance_id=config.provider_instance_id,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _discover_incidents(
        self,
        program_id: str,
        config: IcMDiscoveryConfig,
        existing_registrations: tuple[ChannelRegistration, ...],
        now: datetime,
    ) -> tuple[list[DiscoveredRef], ScopeStatus, list[IntegrationError]]:
        if not config.owning_teams:
            return [], ScopeStatus(
                scope_id=_INCIDENT_SCOPE_ID,
                status=ScopeStatusKind.SUCCESS,
                completeness=DiscoveryCompleteness.INCREMENTAL,
                item_count=0,
            ), []

        from src.m365.icm_client import IcmClient

        try:
            client = IcmClient()
        except (AuthError, Exception) as exc:
            return [], ScopeStatus(
                scope_id=_INCIDENT_SCOPE_ID,
                status=ScopeStatusKind.AUTH_ERROR,
                completeness=DiscoveryCompleteness.PARTIAL,
                item_count=0,
                error_message=str(exc),
            ), [IntegrationError(source="icm", stage="discovery", message=str(exc), retryable=False)]

        existing_incident_ids = {
            r.ref_id for r in existing_registrations if r.ref_kind == "incident"
        }
        refs: list[DiscoveredRef] = []
        errors: list[IntegrationError] = []
        item_count = 0

        try:
            params: dict = {"$top": _INCIDENT_PAGE_SIZE}
            if config.severity_filter:
                sev_values = ",".join(str(s) for s in config.severity_filter)
                params["severity"] = sev_values
            payload = client.list_incidents(params=params)
            incidents = payload.get("items") or payload.get("value") or []
            for raw in incidents:
                if not isinstance(raw, dict):
                    continue
                incident_id = str(raw.get("id") or raw.get("IncidentId") or "")
                if not incident_id:
                    continue
                if incident_id in existing_incident_ids:
                    item_count += 1
                    continue
                owning_team = str(raw.get("owningTeamName") or raw.get("OwningTeamName") or "")
                if config.owning_teams and owning_team not in config.owning_teams:
                    continue
                title = str(raw.get("title") or raw.get("Title") or "")
                severity = raw.get("severity") or raw.get("Severity")
                refs.append(
                    DiscoveredRef(
                        registration=ChannelRegistration(
                            channel="icm",
                            program_id=program_id,
                            ref_id=incident_id,
                            ref_kind="incident",
                            status=RegistrationStatus.ACTIVE,
                            first_discovered_at=now,
                            last_seen_at=now,
                            confidence=1.0,
                            confidence_source="icm_api",
                            provider_instance_id=config.provider_instance_id,
                            ref_title=title or None,
                            metadata={
                                "owning_team": owning_team,
                                "severity": int(severity) if severity is not None else None,
                            },
                        ),
                        bindings=(),
                    )
                )
                item_count += 1
        except Exception as exc:
            errors.append(IntegrationError(source="icm", stage="discovery", message=str(exc), retryable=True))
            is_auth_error = isinstance(exc, AuthError)
            return refs, ScopeStatus(
                scope_id=_INCIDENT_SCOPE_ID,
                status=ScopeStatusKind.AUTH_ERROR if is_auth_error else ScopeStatusKind.ERROR,
                completeness=DiscoveryCompleteness.PARTIAL,
                item_count=item_count,
                error_message=str(exc),
            ), errors

        return refs, ScopeStatus(
            scope_id=_INCIDENT_SCOPE_ID,
            status=ScopeStatusKind.SUCCESS,
            completeness=DiscoveryCompleteness.INCREMENTAL,
            item_count=item_count,
        ), errors


def _build_team_mapping_refs(config: IcMDiscoveryConfig, program_id: str, now: datetime) -> list[DiscoveredRef]:
    refs: list[DiscoveredRef] = []
    for team in config.owning_teams:
        refs.append(
            DiscoveredRef(
                registration=ChannelRegistration(
                    channel="icm",
                    program_id=program_id,
                    ref_id=team,
                    ref_kind="icm_team",
                    status=RegistrationStatus.ACTIVE,
                    first_discovered_at=now,
                    last_seen_at=now,
                    confidence=1.0,
                    confidence_source="static_config",
                    provider_instance_id=config.provider_instance_id,
                    ref_title=team,
                    metadata={"team_name": team},
                ),
                bindings=(),
            )
        )
    return refs
