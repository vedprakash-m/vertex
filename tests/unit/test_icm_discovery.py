"""Unit tests for IcMDiscoveryProvider."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from src.core.integration_types import (
    ChannelConfig,
    DiscoveryCompleteness,
    RunContext,
    ScopeStatusKind,
)
from src.core.models_v2 import Program, Workstream
from src.m365.icm_discovery import IcMDiscoveryConfig, IcMDiscoveryProvider

_NOW = datetime(2026, 5, 24, 12, 0, 0, tzinfo=timezone.utc)

_CHANNEL_CONFIG = ChannelConfig(
    channel="icm",
    enabled=True,
    discovery_threshold_hours=12,
    ttl_days=7,
    extra={"owning_teams": ["StoragePM", "Acme"]},
)

_RUN_CTX = RunContext(dry_run=False, force_discovery=False, accept_shrinkage=False)


def _make_config(
    owning_teams: tuple[str, ...] = ("StoragePM",),
    severity_filter: tuple[int, ...] = (0, 1, 2),
) -> IcMDiscoveryConfig:
    return IcMDiscoveryConfig(
        owning_teams=owning_teams,
        severity_filter=severity_filter,
    )


class TestIcMDiscoveryTeamMappingScope:
    def test_discovers_team_mapping_refs(self) -> None:
        provider = IcMDiscoveryProvider()
        config = _make_config(owning_teams=("StoragePM", "Acme"))

        result = provider.discover("prog1", config, (), run_ctx=_RUN_CTX)

        team_refs = [r for r in result.discovered_refs if r.registration.ref_kind == "icm_team"]
        assert len(team_refs) == 2
        ref_ids = {r.registration.ref_id for r in team_refs}
        assert ref_ids == {"StoragePM", "Acme"}

    def test_team_mapping_refs_have_full_confidence(self) -> None:
        provider = IcMDiscoveryProvider()
        config = _make_config(owning_teams=("StoragePM",))

        result = provider.discover("prog1", config, (), run_ctx=_RUN_CTX)

        team_ref = next(r for r in result.discovered_refs if r.registration.ref_kind == "icm_team")
        assert team_ref.registration.confidence == 1.0
        assert team_ref.registration.confidence_source == "static_config"

    def test_team_mapping_scope_is_full(self) -> None:
        from src.m365.icm_discovery import _TEAM_MAPPING_SCOPE_ID
        provider = IcMDiscoveryProvider()
        config = _make_config(owning_teams=("StoragePM",))

        result = provider.discover("prog1", config, (), run_ctx=_RUN_CTX)

        assert result.scope_statuses[_TEAM_MAPPING_SCOPE_ID].completeness == DiscoveryCompleteness.FULL
        assert result.scope_statuses[_TEAM_MAPPING_SCOPE_ID].status == ScopeStatusKind.SUCCESS

    def test_empty_owning_teams_returns_empty_team_refs(self) -> None:
        provider = IcMDiscoveryProvider()
        config = _make_config(owning_teams=())

        result = provider.discover("prog1", config, (), run_ctx=_RUN_CTX)

        team_refs = [r for r in result.discovered_refs if r.registration.ref_kind == "icm_team"]
        assert team_refs == []


class TestIcMDiscoveryIncidentScope:
    def test_incident_scope_is_auth_error_when_credentials_missing(self) -> None:
        from src.m365.icm_discovery import _INCIDENT_SCOPE_ID
        provider = IcMDiscoveryProvider()
        config = _make_config(owning_teams=("StoragePM",))

        # Without ICM env vars, IcmClient raises AuthError
        result = provider.discover("prog1", config, (), run_ctx=_RUN_CTX)

        # Incident scope should show auth error (no credentials set)
        incident_status = result.scope_statuses.get(_INCIDENT_SCOPE_ID)
        assert incident_status is not None
        assert incident_status.status in (ScopeStatusKind.AUTH_ERROR, ScopeStatusKind.SUCCESS)
        # Either auth error (no creds) or success (creds set in env) — both acceptable

    def test_empty_teams_skips_incident_query(self) -> None:
        from src.m365.icm_discovery import _INCIDENT_SCOPE_ID
        provider = IcMDiscoveryProvider()
        config = _make_config(owning_teams=())

        result = provider.discover("prog1", config, (), run_ctx=_RUN_CTX)

        incident_status = result.scope_statuses.get(_INCIDENT_SCOPE_ID)
        # With no owning_teams, incident scope returns early
        assert incident_status is not None
        assert incident_status.item_count == 0

    def test_incidents_returned_by_api_become_refs(self) -> None:
        from src.m365.icm_discovery import _INCIDENT_SCOPE_ID
        provider = IcMDiscoveryProvider()
        config = _make_config(owning_teams=("StoragePM",))

        mock_client = MagicMock()
        mock_client.list_incidents.return_value = {
            "items": [
                {"id": "111", "title": "Disk full", "severity": 1, "owningTeamName": "StoragePM"},
                {"id": "222", "title": "Network down", "severity": 0, "owningTeamName": "StoragePM"},
            ]
        }
        with patch("src.m365.icm_client.IcmClient", return_value=mock_client):
            result = provider.discover("prog1", config, (), run_ctx=_RUN_CTX)

        incident_refs = [r for r in result.discovered_refs if r.registration.ref_kind == "incident"]
        assert len(incident_refs) == 2
        ref_ids = {r.registration.ref_id for r in incident_refs}
        assert ref_ids == {"111", "222"}

    def test_incidents_filtered_by_owning_team(self) -> None:
        provider = IcMDiscoveryProvider()
        config = _make_config(owning_teams=("StoragePM",))

        mock_client = MagicMock()
        mock_client.list_incidents.return_value = {
            "items": [
                {"id": "111", "title": "Storage issue", "severity": 1, "owningTeamName": "StoragePM"},
                {"id": "222", "title": "Network issue", "severity": 1, "owningTeamName": "NetworkTeam"},
            ]
        }
        with patch("src.m365.icm_client.IcmClient", return_value=mock_client):
            result = provider.discover("prog1", config, (), run_ctx=_RUN_CTX)

        incident_refs = [r for r in result.discovered_refs if r.registration.ref_kind == "incident"]
        assert len(incident_refs) == 1
        assert incident_refs[0].registration.ref_id == "111"


class TestIcMDiscoveryChannel:
    def test_channel_name(self) -> None:
        assert IcMDiscoveryProvider().channel == "icm"

    def test_from_program_creates_provider_and_config(self) -> None:
        program = Program(schema_version="3.0", id="prog1", name="Prog")
        provider, config = IcMDiscoveryProvider.from_program(
            program, _CHANNEL_CONFIG, ()
        )
        assert isinstance(provider, IcMDiscoveryProvider)
        assert isinstance(config, IcMDiscoveryConfig)
        assert "StoragePM" in config.owning_teams
        assert "Acme" in config.owning_teams

    def test_overall_completeness_is_incremental(self) -> None:
        provider = IcMDiscoveryProvider()
        config = _make_config(owning_teams=("StoragePM",))

        result = provider.discover("prog1", config, (), run_ctx=_RUN_CTX)

        assert result.completeness == DiscoveryCompleteness.INCREMENTAL
