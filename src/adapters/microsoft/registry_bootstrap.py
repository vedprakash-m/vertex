from __future__ import annotations

from src.core.ado_discovery import ADODiscoveryProvider
from src.core.ado_hydration import ADOHydrationProvider
from src.core.ado_signal_extractor import ADOSignalExtractor
from src.core.email_signal_extractor import EmailSignalExtractor
from src.core.icm_signal_extractor import IcMSignalExtractor
from src.core.kusto_discovery import KustoDiscoveryProvider
from src.core.kusto_hydration import KustoHydrationProvider
from src.core.kusto_signal_extractor import KustoSignalExtractor
from src.core.provider_registry import ProviderRegistration, ProviderRegistry
from src.core.teams_signal_extractor import TeamsSignalExtractor
from src.m365.email_discovery import EmailDiscoveryProvider
from src.m365.email_hydration import EmailHydrationProvider
from src.m365.icm_discovery import IcMDiscoveryProvider
from src.m365.icm_hydration import IcMHydrationProvider
from src.m365.teams_discovery import TeamsDiscoveryProvider
from src.m365.teams_hydration import TeamsHydrationProvider


def bootstrap_microsoft_providers(registry: ProviderRegistry) -> None:
    registry.register(
        ProviderRegistration(
            channel="ado",
            discovery_cls=ADODiscoveryProvider,
            hydration_cls=ADOHydrationProvider,
            signal_extractor_cls=ADOSignalExtractor,
        )
    )
    registry.register(
        ProviderRegistration(
            channel="kusto",
            discovery_cls=KustoDiscoveryProvider,
            hydration_cls=KustoHydrationProvider,
            signal_extractor_cls=KustoSignalExtractor,
        )
    )
    registry.register(
        ProviderRegistration(
            channel="teams",
            discovery_cls=TeamsDiscoveryProvider,
            hydration_cls=TeamsHydrationProvider,
            signal_extractor_cls=TeamsSignalExtractor,
        )
    )
    registry.register(
        ProviderRegistration(
            channel="email",
            discovery_cls=EmailDiscoveryProvider,
            hydration_cls=EmailHydrationProvider,
            signal_extractor_cls=EmailSignalExtractor,
        )
    )
    registry.register(
        ProviderRegistration(
            channel="icm",
            discovery_cls=IcMDiscoveryProvider,
            hydration_cls=IcMHydrationProvider,
            signal_extractor_cls=IcMSignalExtractor,
        )
    )
