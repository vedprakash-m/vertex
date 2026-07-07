"""FR-SG-48: Connector registry mapping connector_type → implementation class.

D-23: provides `register_with_provider_registry(registry)` so callers
can fold the connectors into the unified `ProviderRegistry` they
already build for the gather-time channel path. The legacy
`CONNECTOR_REGISTRY` dict is retained for back-compat with code that
imports it directly (e.g. tests).
"""

from src.core.connectors.github_issues import GitHubIssuesConnector
from src.core.connectors.sharepoint_lists import SharePointListsConnector
from src.core.external_connector import ExternalConnector

CONNECTOR_REGISTRY: dict[str, type[ExternalConnector]] = {
    "github_issues": GitHubIssuesConnector,
    "sharepoint_lists": SharePointListsConnector,
}


def register_with_provider_registry(registry) -> None:
    """D-23: register all connectors in `CONNECTOR_REGISTRY` with the
    provided `ProviderRegistry` instance. Idempotent: re-registering
    a connector type is a no-op (the registry raises on duplicate
    registration, which we catch and ignore).

    Why:** keeps the legacy `CONNECTOR_REGISTRY` as the single
    source-of-truth for what connector types exist, while also
    exposing them through the unified registry. The caller is
    responsible for the registry lifecycle (typically
    `build_provider_registry()` from `src/commands/channel_wiring.py`).
    **How to apply:** in the gather-time path, after building the
    provider registry, call
    `register_with_provider_registry(registry)`. The
    `make_connector` factory in `external_connector.py` resolves
    connectors through the unified registry, so any registry that
    hasn't called this helper will fail connector resolution with
    a clear error.
    """
    from src.core.provider_registry import ProviderRegistry  # avoid circular import
    if not isinstance(registry, ProviderRegistry):
        raise TypeError(
            f"register_with_provider_registry expected a ProviderRegistry, "
            f"got {type(registry).__name__}"
        )
    for connector_type, connector_cls in CONNECTOR_REGISTRY.items():
        if connector_type in registry.connector_registrations:
            continue
        try:
            registry.register_connector(connector_type, connector_cls)
        except ValueError:
            # Re-registration from a duplicate import path; ignore.
            pass


__all__ = [
    "CONNECTOR_REGISTRY",
    "GitHubIssuesConnector",
    "SharePointListsConnector",
    "register_with_provider_registry",
]
