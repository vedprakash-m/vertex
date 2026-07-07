"""Channel discovery setup helpers for the integration command (D-13).

Extracted from the ``integration.py`` god module (§28.4 strangler fig): channel
config/binding resolution, workstream projection, candidate-store construction,
and single-channel discovery execution. ``integration.py`` re-imports these so
its attribute surface and call sites are unchanged.

NOTE (test seam): the discover/seed-id/candidate tests monkeypatch the provider
dependencies ``resolve_channel_config`` / ``resolve_channel_bindings`` — they are
patched at THIS module (where the helpers bind them), not at ``integration``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from src.commands.channel_wiring import resolve_channel_bindings, resolve_channel_config, resolve_channel_configs
from src.core.channel_registry_store import ChannelRegistryStore, normalize_discovery_result_provider_instance
from src.core.integration_protocol import DiscoveryProvider
from src.core.integration_types import ChannelBinding, ChannelConfig, RunContext
from src.core.models_v2 import Program
from src.core.program_fact_store import load_program_facts, project_workstreams
from src.core.program_paths import get_channel_registry_path
from src.core.source_candidate_store import SourceCandidateStore


def _channel_config(program: Program, channel: str, *, programs_root: Path):
    config = resolve_channel_config(program, channel, programs_root=programs_root)
    if config is not None:
        return config
    raise ValueError(f"Program is missing channel config for '{channel}'")


def _channel_exists(program: Program, channel: str, *, programs_root: Path) -> bool:
    return resolve_channel_config(program, channel, programs_root=programs_root) is not None


def _discover_channel_configs(program: Program, channel: str | None, *, programs_root: Path):
    configs = resolve_channel_configs(program, programs_root=programs_root)
    if channel is not None:
        config = _channel_config(program, channel, programs_root=programs_root)
        return (config,) if config.enabled else ()
    return tuple(config for config in configs if config.enabled)


def _discover_channel_bindings(program: Program, selected_configs: tuple[ChannelConfig, ...], *, programs_root: Path):
    workstreams = _load_workstreams(program.id, programs_root=programs_root)
    bindings = resolve_channel_bindings(program, workstreams, programs_root=programs_root)
    bindings_by_channel = {binding.config.channel: binding for binding in bindings}
    selected_bindings = []
    for config in selected_configs:
        binding = bindings_by_channel.get(config.channel)
        if binding is None:
            raise ValueError(f"Program is missing channel binding for '{config.channel}'")
        selected_bindings.append(binding)
    return tuple(selected_bindings)


def _run_discovery(
    *,
    program: str,
    binding: ChannelBinding,
    store: ChannelRegistryStore | None,
    provider_instance_id: str,
    run_ctx: RunContext,
):
    selected_channel = binding.config.channel
    discovery_provider = cast(DiscoveryProvider[Any], binding.discovery_provider)
    result = discovery_provider.discover(
        program,
        binding.discovery_config,
        store.active_registrations(selected_channel, provider_instance_id=provider_instance_id) if store is not None else (),
        run_ctx=run_ctx,
    )
    return normalize_discovery_result_provider_instance(
        result,
        expected_provider_instance_id=provider_instance_id,
    )


def _load_workstreams(program: str, *, programs_root: Path):
    return project_workstreams(
        load_program_facts(
            program,
            programs_root=programs_root,
            fact_types=("workstream.entry",),
        )
    )


def _candidate_store(program: str, programs_root: Path) -> SourceCandidateStore:
    return SourceCandidateStore(get_channel_registry_path(program, programs_root=programs_root), program)
