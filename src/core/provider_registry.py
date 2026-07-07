from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.core.integration_types import ChannelBinding, ChannelConfig
from src.core.models_v2 import Program, Workstream


LegacyProviderTuple = tuple[type[Any], type[Any], type[Any]]


@dataclass(frozen=True, slots=True)
class ProviderRegistration:
    """Transitional registration for the current discovery/hydration/extractor triplet model."""

    channel: str
    discovery_cls: type[Any]
    hydration_cls: type[Any]
    signal_extractor_cls: type[Any]


# D-23: `ExternalConnector` is folded into the unified `ProviderRegistry`
# as the extension mechanism for non-Microsoft (and non-M365) sources.
# We use a forward-reference string for the connector class type to
# avoid a circular import (the connector module imports from
# `external_connector` which would otherwise need to import
# `provider_registry`).
ExternalConnectorClass = Any


class ProviderNotFoundError(LookupError):
    pass


class ProviderRegistry:
    def __init__(self) -> None:
        self._registrations: dict[str, ProviderRegistration] = {}
        # D-23: connector types registered alongside channels so a
        # single registry drives both gather-time channels and
        # slice-contract external connectors. Keyed by the
        # `connector_type` string from `ExternalConnectorConfig`.
        self._connector_registrations: dict[str, type[Any]] = {}

    def register(self, registration: ProviderRegistration) -> None:
        channel = registration.channel.strip()
        if not channel:
            raise ValueError("Provider registration channel must be non-empty.")
        if channel in self._registrations:
            raise ValueError(f"Provider channel '{channel}' is already registered.")
        self._registrations[channel] = registration

    def register_connector(self, connector_type: str, connector_cls: type[Any]) -> None:
        """D-23: register an `ExternalConnector` subclass under a
        `connector_type` key (the same string used in
        `slice_contracts.yaml`'s `external_connectors[].connector_type`
        and in `ExternalConnectorConfig.connector_type`).

        Why:** the previous design had two registries —
        `ProviderRegistry` for gather-time channels (Teams, ADO, Kusto,
        IcM) and `CONNECTOR_REGISTRY` in `src/core/connectors/` for
        slice-contract external connectors (GitHub, SharePoint). The
        two were wired differently (M365 channels go through
        `ProviderFactory.create_binding`; external connectors go
        through `make_connector` in `external_connector.py`). D-23
        unifies them so the registry is the single point of extension
        for both kinds of source.
        **How to apply:** new connector implementations register via
        `ProviderRegistry().register_connector("my_connector_type",
        MyConnector)` at module import time (e.g. inside
        `src/core/connectors/__init__.py`'s
        `_register_with_provider_registry()`). The legacy
        `CONNECTOR_REGISTRY` dict is retained for back-compat; new
        code should resolve connectors through
        `ProviderRegistry.resolve_connector()`.
        """
        normalized = connector_type.strip()
        if not normalized:
            raise ValueError("Connector type must be non-empty.")
        if normalized in self._connector_registrations:
            raise ValueError(
                f"Connector type '{normalized}' is already registered."
            )
        self._connector_registrations[normalized] = connector_cls

    def resolve(self, channel: str) -> ProviderRegistration:
        normalized = channel.strip()
        registration = self._registrations.get(normalized)
        if registration is None:
            raise ProviderNotFoundError(f"Unsupported integration channel '{channel}'")
        return registration

    def resolve_connector(self, connector_type: str) -> type[Any]:
        """D-23: resolve a registered `ExternalConnector` subclass by
        `connector_type` string. Raises `ValueError` if the type is
        not registered (matches the prior `make_connector` error
        contract)."""
        normalized = connector_type.strip()
        connector_cls = self._connector_registrations.get(normalized)
        if connector_cls is None:
            raise ValueError(
                f"Unknown connector type {normalized!r}. "
                f"Registered types: {sorted(self._connector_registrations)}"
            )
        return connector_cls

    def channels(self) -> tuple[str, ...]:
        return tuple(self._registrations)

    def connector_types(self) -> tuple[str, ...]:
        """D-23: return the registered connector types, newest first.
        Used by doctor checks and admin commands to surface what
        external connector types are available."""
        return tuple(self._connector_registrations)

    @property
    def connector_registrations(self) -> dict[str, type[Any]]:
        """D-23: read-only view of the connector-type → class mapping.
        Returned as a copy so callers cannot mutate the registry's
        internal state directly."""
        return dict(self._connector_registrations)


class ProviderFactory:
    def __init__(self, registry: ProviderRegistry) -> None:
        self._registry = registry

    @classmethod
    def from_registry(cls, registry: ProviderRegistry) -> ProviderFactory:
        return cls(registry)

    @classmethod
    def from_legacy_registry(cls, registry: dict[str, LegacyProviderTuple]) -> ProviderFactory:
        provider_registry = ProviderRegistry()
        for channel, registration in registry.items():
            provider_registry.register(
                ProviderRegistration(
                    channel=channel,
                    discovery_cls=registration[0],
                    hydration_cls=registration[1],
                    signal_extractor_cls=registration[2],
                )
            )
        return cls(provider_registry)

    def supported_channels(self) -> frozenset[str]:
        return frozenset(self._registry.channels())

    def resolve(self, channel: str) -> ProviderRegistration:
        return self._registry.resolve(channel)

    def create_binding(
        self,
        program: Program,
        workstreams: tuple[Workstream, ...],
        config: ChannelConfig,
        *,
        programs_root: Path,
    ) -> ChannelBinding:
        registration = self.resolve(config.channel)
        discovery_provider, discovery_config = registration.discovery_cls.from_program(  # type: ignore[attr-defined]
            program,
            config,
            workstreams,
            programs_root=programs_root,
        )
        hydration_provider, hydration_config = registration.hydration_cls.from_program(  # type: ignore[attr-defined]
            program,
            config,
            workstreams,
            programs_root=programs_root,
        )
        return ChannelBinding(
            config=config,
            discovery_provider=discovery_provider,
            hydration_provider=hydration_provider,
            signal_extractor=registration.signal_extractor_cls(),
            discovery_config=discovery_config,
            hydration_config=hydration_config,
        )

    def from_program(
        self,
        program: Program,
        workstreams: tuple[Workstream, ...],
        channel_configs: tuple[ChannelConfig, ...],
        *,
        programs_root: Path,
    ) -> tuple[ChannelBinding, ...]:
        return tuple(
            self.create_binding(program, workstreams, config, programs_root=programs_root)
            for config in channel_configs
            if config.enabled
        )
