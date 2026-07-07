"""Unit tests for IcMHydrationProvider."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from src.core.integration_types import (
    ChannelRegistration,
    HydrationMode,
    RegistrationStatus,
    RunContext,
)
from src.m365.icm_hydration import IcMHydrationConfig, IcMHydrationProvider

_NOW = datetime(2026, 5, 24, 12, 0, 0, tzinfo=timezone.utc)
_RUN_CTX = RunContext(dry_run=False, force_discovery=False, accept_shrinkage=False)


def _incident_reg(
    ref_id: str = "98765",
    workstream_ids: tuple[str, ...] = ("ws-a",),
) -> ChannelRegistration:
    return ChannelRegistration(
        channel="icm",
        program_id="prog1",
        provider_instance_id="default",
        ref_id=ref_id,
        ref_kind="incident",
        status=RegistrationStatus.ACTIVE,
        first_discovered_at=_NOW,
        last_seen_at=_NOW,
        workstream_ids=workstream_ids,
    )


def _icm_team_reg(ref_id: str = "StoragePM") -> ChannelRegistration:
    return ChannelRegistration(
        channel="icm",
        program_id="prog1",
        provider_instance_id="default",
        ref_id=ref_id,
        ref_kind="icm_team",
        status=RegistrationStatus.ACTIVE,
        first_discovered_at=_NOW,
        last_seen_at=_NOW,
    )


class TestIcMHydrationSkipsTeamRefs:
    def test_icm_team_refs_are_skipped(self) -> None:
        provider = IcMHydrationProvider()
        regs = (_icm_team_reg(),)
        config = IcMHydrationConfig()

        result = provider.hydrate(regs, _NOW, "prog1", config, run_ctx=_RUN_CTX)

        assert result.resources.incident_states == ()
        assert result.hydrated_ref_ids == ()
        assert result.failed_ref_ids == ()
        assert result.errors == ()

    def test_mixed_regs_only_hydrates_incident_refs(self) -> None:
        provider = IcMHydrationProvider()
        # icm_team ref should be skipped
        regs = (_icm_team_reg(), _incident_reg())
        config = IcMHydrationConfig()

        mock_client = MagicMock()
        mock_client.list_incidents.return_value = {
            "items": [
                {
                    "id": "98765",
                    "title": "Disk full",
                    "severity": 1,
                    "status": "Active",
                    "owningTeamName": "StoragePM",
                    "modifiedAt": "2026-05-24T12:00:00Z",
                }
            ]
        }
        with patch("src.m365.icm_client.IcmClient", return_value=mock_client):
            result = provider.hydrate(regs, _NOW, "prog1", config, run_ctx=_RUN_CTX)

        assert len(result.resources.incident_states) == 1
        assert result.hydrated_ref_ids == (("98765", "incident"),)


class TestIcMHydrationAuthError:
    def test_auth_error_returns_empty_output_with_failed_refs(self) -> None:
        from src.core.exceptions import AuthError

        provider = IcMHydrationProvider()
        regs = (_incident_reg(),)
        config = IcMHydrationConfig()

        with patch("src.m365.icm_client.IcmClient", side_effect=AuthError("No credentials")):
            result = provider.hydrate(regs, _NOW, "prog1", config, run_ctx=_RUN_CTX)

        assert result.resources.incident_states == ()
        assert ("98765", "incident") in result.failed_ref_ids


class TestIcMHydrationIncidentParsing:
    def test_hydrates_incident_with_full_data(self) -> None:
        provider = IcMHydrationProvider()
        regs = (_incident_reg(workstream_ids=("ws-a",)),)
        config = IcMHydrationConfig()

        mock_client = MagicMock()
        mock_client.list_incidents.return_value = {
            "items": [
                {
                    "id": "98765",
                    "title": "Disk full on storage node",
                    "severity": 1,
                    "status": "Active",
                    "owningTeamName": "StoragePM",
                    "modifiedAt": "2026-05-24T12:00:00Z",
                }
            ]
        }
        with patch("src.m365.icm_client.IcmClient", return_value=mock_client):
            result = provider.hydrate(regs, _NOW, "prog1", config, run_ctx=_RUN_CTX)

        state = result.resources.incident_states[0]
        assert state.incident_id == "98765"
        assert state.title == "Disk full on storage node"
        assert state.severity == 1
        assert state.status == "Active"
        assert state.owning_team == "StoragePM"
        assert state.workstream_ids == ("ws-a",)

    def test_fallback_to_registration_when_api_returns_empty(self) -> None:
        provider = IcMHydrationProvider()
        regs = (_incident_reg(ref_id="00001"),)
        config = IcMHydrationConfig()

        mock_client = MagicMock()
        mock_client.list_incidents.return_value = {"items": []}  # empty list
        with patch("src.m365.icm_client.IcmClient", return_value=mock_client):
            result = provider.hydrate(regs, _NOW, "prog1", config, run_ctx=_RUN_CTX)

        # Falls back to registration metadata
        assert len(result.resources.incident_states) == 1
        assert result.resources.incident_states[0].incident_id == "00001"

    def test_multiple_incidents_hydrated_independently(self) -> None:
        provider = IcMHydrationProvider()
        regs = (_incident_reg(ref_id="111"), _incident_reg(ref_id="222"))
        config = IcMHydrationConfig()

        mock_client = MagicMock()
        mock_client.list_incidents.return_value = {
            "items": [
                {"id": "111", "title": "T1", "severity": 1, "status": "Active",
                 "owningTeamName": "Team", "modifiedAt": "2026-05-24T12:00:00Z"},
            ]
        }
        with patch("src.m365.icm_client.IcmClient", return_value=mock_client):
            result = provider.hydrate(regs, _NOW, "prog1", config, run_ctx=_RUN_CTX)

        assert len(result.resources.incident_states) == 2
        assert result.api_call_count == 2


class TestIcMHydrationChannel:
    def test_channel_name(self) -> None:
        assert IcMHydrationProvider().channel == "icm"
