"""FR-SG-48: Abstract external connector interface for non-ADO dependencies.

D-23: `make_connector` now resolves connector types through the unified
`ProviderRegistry` (which `src/core/connectors/__init__.py` populates at
import time). The legacy `CONNECTOR_REGISTRY` dict is retained for
back-compat with direct imports (e.g. tests, third-party code).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from src.core.connector_config import ExternalConnectorConfig
from src.core.external_dependency import ExternalDependency


class ExternalConnector(ABC):
    """Read-only polling interface for non-ADO external dependency sources."""

    def __init__(self, config: ExternalConnectorConfig) -> None:
        self._config = config

    @property
    def config(self) -> ExternalConnectorConfig:
        return self._config

    @abstractmethod
    def poll(self) -> ExternalDependency:
        """Fetch current state and return an ExternalDependency snapshot."""
        ...

    @abstractmethod
    def health_check(self) -> bool:
        """Return True if the remote source is reachable (no auth required)."""
        ...


def make_connector(config: ExternalConnectorConfig) -> ExternalConnector:
    """Registry factory: instantiate the right connector for a config entry.

    D-23: delegates to the unified `ProviderRegistry.resolve_connector`.
    The legacy `CONNECTOR_REGISTRY` is still populated (see
    `src/core/connectors/__init__.py`) so direct imports keep working
    during the migration window.

    Implementation note: the gather-time path builds a
    `ProviderRegistry` via `build_provider_registry()` and registers
    connectors into that specific instance. For callers that reach
    `make_connector` outside the gather-time path (e.g. a standalone
    CLI invocation or test that imports `external_connector`
    directly), we provide a process-level fallback: a private
    `_DEFAULT_CONNECTOR_REGISTRY` that has the connectors registered.
    This keeps the unification contract working even when the
    gather-time bootstrap hasn't run.
    """
    # Lazy import to avoid circular import (provider_registry imports
    # from integration_types which transitively imports from
    # core modules that may import from external_connector).
    from src.core.connectors import register_with_provider_registry  # noqa: PLC0415
    from src.core.provider_registry import ProviderRegistry  # noqa: PLC0415

    registry = _get_or_init_default_registry(ProviderRegistry)
    register_with_provider_registry(registry)
    connector_cls = registry.resolve_connector(config.connector_type)
    return connector_cls(config)


_DEFAULT_CONNECTOR_REGISTRY = None
_PROVIDER_REGISTRY_CLS = None


def _get_or_init_default_registry(provider_registry_cls):
    """D-23: process-level singleton `ProviderRegistry` for callers
    that don't have a gather-time-built registry in scope. The
    singleton is lazy-initialized and re-used for the process
    lifetime; registration is idempotent so callers can also
    register additional connector types without resetting the
    registry.

    The class is passed in as an argument to avoid a top-level
    import cycle (`ProviderRegistry` is imported lazily here).
    """
    global _DEFAULT_CONNECTOR_REGISTRY
    if _DEFAULT_CONNECTOR_REGISTRY is None:
        _DEFAULT_CONNECTOR_REGISTRY = provider_registry_cls()
    return _DEFAULT_CONNECTOR_REGISTRY
