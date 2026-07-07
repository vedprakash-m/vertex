from __future__ import annotations

from unittest.mock import MagicMock

from src.commands.channel_wiring import build_provider_registry
from src.core.ado_discovery import ADODiscoveryProvider
from src.core.ado_hydration import ADOHydrationProvider
from src.core.ado_signal_extractor import ADOSignalExtractor
from src.core.email_signal_extractor import EmailSignalExtractor
from src.core.icm_signal_extractor import IcMSignalExtractor
from src.core.integration_protocol import DiscoveryProvider, HydrationProvider, SignalExtractor
from src.core.integration_types import ProviderCapability
from src.core.kusto_discovery import KustoDiscoveryProvider
from src.core.kusto_hydration import KustoHydrationProvider
from src.core.kusto_signal_extractor import KustoSignalExtractor
from src.core.provider_registry import ProviderNotFoundError, ProviderRegistration
from src.core.teams_signal_extractor import TeamsSignalExtractor
from src.m365.email_discovery import EmailDiscoveryProvider
from src.m365.email_hydration import EmailHydrationProvider
from src.m365.icm_discovery import IcMDiscoveryProvider
from src.m365.icm_hydration import IcMHydrationProvider
from src.m365.teams_discovery import TeamsDiscoveryProvider
from src.m365.teams_hydration import TeamsHydrationProvider


def _provider_instances(channel: str) -> tuple[object, object, object]:
    if channel == "ado":
        return ADODiscoveryProvider(MagicMock()), ADOHydrationProvider(MagicMock()), ADOSignalExtractor()
    if channel == "kusto":
        return (
            KustoDiscoveryProvider(query_loader=lambda program_id, programs_root: ()),
            KustoHydrationProvider(executor=lambda query: [], query_loader=lambda program_id, programs_root: ()),
            KustoSignalExtractor(),
        )
    if channel == "teams":
        return (
            TeamsDiscoveryProvider(MagicMock(), MagicMock()),
            TeamsHydrationProvider(MagicMock(), MagicMock()),
            TeamsSignalExtractor(),
        )
    if channel == "email":
        return EmailDiscoveryProvider(), EmailHydrationProvider(MagicMock()), EmailSignalExtractor()
    if channel == "icm":
        return IcMDiscoveryProvider(), IcMHydrationProvider(), IcMSignalExtractor()
    raise AssertionError(f"Unhandled provider channel: {channel}")


def _assert_capability_contract(capability: ProviderCapability, *, channel: str) -> None:
    assert capability.channel == channel
    assert capability.discovery_modes
    assert capability.hydration_modes
    assert capability.max_batch_size > 0
    assert capability.retry_max_attempts >= 1
    assert capability.retry_backoff_seconds >= 0
    assert capability.timeout_seconds > 0
    assert capability.privacy_class


def test_provider_registry_resolves_bundled_microsoft_pack_channels() -> None:
    registry = build_provider_registry()

    for channel in ("ado", "kusto", "teams", "email", "icm"):
        registration = registry.resolve(channel)
        assert isinstance(registration, ProviderRegistration)
        assert registration.channel == channel


def test_provider_registry_rejects_unknown_channel() -> None:
    registry = build_provider_registry()

    try:
        registry.resolve("nonexistent_channel")
    except ProviderNotFoundError as error:
        assert "nonexistent_channel" in str(error)
    else:  # pragma: no cover
        raise AssertionError("Expected ProviderNotFoundError for unknown channel")


def test_provider_registry_entries_satisfy_shared_protocols() -> None:
    registry = build_provider_registry()

    for channel in ("ado", "kusto", "teams", "email", "icm"):
        registration = registry.resolve(channel)
        discovery, hydration, extractor = _provider_instances(channel)

        assert isinstance(discovery, registration.discovery_cls)
        assert isinstance(hydration, registration.hydration_cls)
        assert isinstance(extractor, registration.signal_extractor_cls)

        assert isinstance(discovery, DiscoveryProvider)
        assert isinstance(hydration, HydrationProvider)
        assert isinstance(extractor, SignalExtractor)

        assert discovery.channel == channel
        assert hydration.channel == channel
        assert extractor.channel == channel

        _assert_capability_contract(discovery.capability, channel=channel)
        _assert_capability_contract(hydration.capability, channel=channel)
