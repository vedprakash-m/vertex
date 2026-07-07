from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from src.adapters.microsoft.registry_bootstrap import bootstrap_microsoft_providers
from src.core.ado_discovery import ADODiscoveryProvider
from src.core.ado_hydration import ADOHydrationProvider
from src.core.ado_signal_extractor import ADOSignalExtractor
from src.core.config_loader import PROGRAMS_ROOT
from src.core.connectors import register_with_provider_registry as _register_connectors_with_registry
from src.core.email_signal_extractor import EmailSignalExtractor
from src.core.integration_types import ChannelBinding, ChannelConfig
from src.core.icm_signal_extractor import IcMSignalExtractor
from src.core.kusto_discovery import KustoDiscoveryProvider
from src.core.kusto_hydration import KustoHydrationProvider
from src.core.kusto_signal_extractor import KustoSignalExtractor
from src.core.models_v2 import Program, Workstream
from src.core.provider_registry import ProviderFactory, ProviderRegistry
from src.core.teams_signal_extractor import TeamsSignalExtractor
from src.core.yaml_utils import load_yaml_mapping
from src.m365.icm_discovery import IcMDiscoveryProvider
from src.m365.icm_hydration import IcMHydrationProvider
from src.m365.email_discovery import EmailDiscoveryProvider
from src.m365.email_hydration import EmailHydrationProvider
from src.m365.teams_discovery import TeamsDiscoveryProvider
from src.m365.teams_hydration import TeamsHydrationProvider


PROVIDER_REGISTRY: dict[str, tuple[type, type, type]] = {
    "ado": (ADODiscoveryProvider, ADOHydrationProvider, ADOSignalExtractor),
    "kusto": (KustoDiscoveryProvider, KustoHydrationProvider, KustoSignalExtractor),
    "teams": (TeamsDiscoveryProvider, TeamsHydrationProvider, TeamsSignalExtractor),
    "email": (EmailDiscoveryProvider, EmailHydrationProvider, EmailSignalExtractor),
    "icm": (IcMDiscoveryProvider, IcMHydrationProvider, IcMSignalExtractor),
}

_REGISTRY_MODE_ENV = "VERTEX_PROVIDER_REGISTRY"


def build_provider_registry() -> ProviderRegistry:
    registry = ProviderRegistry()
    bootstrap_microsoft_providers(registry)
    # D-23: also register external connectors in the same registry
    # so `make_connector` (which resolves through this unified
    # registry) sees the connector types when invoked from the
    # gather-time path.
    _register_connectors_with_registry(registry)
    return registry


def build_provider_factory() -> ProviderFactory:
    if _provider_registry_mode() == "legacy":
        return ProviderFactory.from_legacy_registry(PROVIDER_REGISTRY)
    return ProviderFactory.from_registry(build_provider_registry())


def resolve_channel_configs(program: Program, *, programs_root: Path = PROGRAMS_ROOT) -> tuple[ChannelConfig, ...]:
    raw_program = load_yaml_mapping(programs_root / program.id / "program.yaml")
    raw_channels = raw_program.get("channels")
    if raw_channels is None:
        return _default_channel_configs(program)
    if not isinstance(raw_channels, dict):
        raise ValueError(f"program.yaml channels must be a mapping for program '{program.id}'")

    # Read provider-pack channel defaults (three-layer resolution:
    # hardcoded < provider_pack < explicit channel config).
    raw_provider_pack = raw_program.get("provider_pack")
    pack_channels: dict[str, dict[str, Any]] = {}
    if isinstance(raw_provider_pack, dict):
        pack_channels = raw_provider_pack.get("channels") or {}

    configs: list[ChannelConfig] = []
    provider_factory = build_provider_factory()
    for channel, raw_config in raw_channels.items():
        if not isinstance(channel, str) or not isinstance(raw_config, dict):
            raise ValueError(f"Invalid channel config for program '{program.id}'")
        if channel not in provider_factory.supported_channels():
            raise ValueError(f"Unsupported integration channel '{channel}' for program '{program.id}'")

        pack_config = pack_channels.get(channel, {})

        configs.append(
            ChannelConfig(
                channel=channel,
                enabled=bool(raw_config.get("enabled", True)),
                discovery_threshold_hours=int(raw_config.get(
                    "discovery_threshold_hours",
                    pack_config.get("discovery_threshold_hours", 24),
                )),
                ttl_days=_optional_int(raw_config.get(
                    "ttl_days",
                    pack_config.get("ttl_days", 30),
                )),
                extra=_merged_channel_extra(raw_config, pack_config),
            )
        )
    return tuple(configs)


def resolve_channel_bindings(
    program: Program,
    workstreams: tuple[Workstream, ...],
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[ChannelBinding, ...]:
    provider_factory = build_provider_factory()
    return provider_factory.from_program(
        program,
        workstreams,
        resolve_channel_configs(program, programs_root=programs_root),
        programs_root=programs_root,
    )


def resolve_channel_config(
    program: Program,
    channel: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> ChannelConfig | None:
    for config in resolve_channel_configs(program, programs_root=programs_root):
        if config.channel == channel:
            return config
    return None


def resolve_channel_binding(
    program: Program,
    workstreams: tuple[Workstream, ...],
    channel: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> ChannelBinding | None:
    for binding in resolve_channel_bindings(program, workstreams, programs_root=programs_root):
        if binding.config.channel == channel:
            return binding
    return None


def _default_channel_configs(program: Program) -> tuple[ChannelConfig, ...]:
    del program
    return ()


def _provider_registry_mode() -> str:
    raw_value = os.environ.get(_REGISTRY_MODE_ENV, "registry").strip().lower()
    if raw_value in {"", "registry"}:
        return "registry"
    if raw_value == "legacy":
        return "legacy"
    raise ValueError(f"{_REGISTRY_MODE_ENV} must be 'legacy' or 'registry'.")


def _optional_int(raw_value: Any) -> int | None:
    if raw_value is None:
        return None
    return int(raw_value)


_ScalarValue = str | int | float | bool | None
_FlatValue = _ScalarValue | list[_ScalarValue]


def _is_flat_value(value: Any) -> bool:
    if isinstance(value, list):
        return all(isinstance(v, (str, int, float, bool, type(None))) for v in value)
    return isinstance(value, (str, int, float, bool, type(None)))


def _flat_extra(raw_extra: Any) -> dict[str, _FlatValue] | None:
    if raw_extra is None:
        return None
    if not isinstance(raw_extra, dict):
        raise ValueError("channel extra config must be a mapping")
    extra: dict[str, _FlatValue] = {}
    for key, value in raw_extra.items():
        if not isinstance(key, str) or not _is_flat_value(value):
            raise ValueError("channel extra config must be JSON-flat")
        extra[key] = value
    return extra


def _channel_extra(raw_config: dict[str, Any]) -> dict[str, _FlatValue] | None:
    extra: dict[str, _FlatValue] = {}
    explicit_extra = _flat_extra(raw_config.get("extra"))
    if explicit_extra is not None:
        extra.update(explicit_extra)
    for key, value in raw_config.items():
        if key in {"enabled", "discovery_threshold_hours", "ttl_days", "extra"}:
            continue
        if not isinstance(key, str) or not _is_flat_value(value):
            raise ValueError("channel config fields beyond the standard keys must be JSON-flat")
        extra[key] = value
    return extra or None


def _merged_channel_extra(
    raw_config: dict[str, Any],
    pack_config: dict[str, Any],
) -> dict[str, _FlatValue] | None:
    """Merge provider-pack extras with channel-level extras.

    Pack defaults are applied first; explicit channel values override.
    """
    extra: dict[str, _FlatValue] = {}
    # Pack-level extras first
    pack_extra = _flat_extra(pack_config.get("extra"))
    if pack_extra is not None:
        extra.update(pack_extra)
    for key, value in pack_config.items():
        if key in {"enabled", "discovery_threshold_hours", "ttl_days", "extra"}:
            continue
        if isinstance(key, str) and _is_flat_value(value):
            extra[key] = value
    # Channel-level extras override pack defaults
    channel_extra = _flat_extra(raw_config.get("extra"))
    if channel_extra is not None:
        extra.update(channel_extra)
    for key, value in raw_config.items():
        if key in {"enabled", "discovery_threshold_hours", "ttl_days", "extra"}:
            continue
        if isinstance(key, str) and _is_flat_value(value):
            extra[key] = value
    return extra or None
