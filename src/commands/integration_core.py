from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.commands.integration_discovery import _load_workstreams
from src.core.channel_registry_store import ChannelRegistryStore
from src.core.edition_resolver import _parse_program
from src.core.m365_registry_store import load_m365_registry
from src.core.program_paths import get_channel_registry_path
from src.core.source_candidate_store import SourceCandidateStore
from src.core.yaml_utils import load_yaml_mapping


def _store_impl(
    program: str,
    programs_root: Path,
    *,
    ensure_schema: bool = True,
) -> ChannelRegistryStore:
    # Write-capable store handle → canonical write getter (R-14, no fallback).
    return ChannelRegistryStore(
        get_channel_registry_path(program, programs_root=programs_root),
        program,
        ensure_schema=ensure_schema,
    )


def _load_program_impl(program: str, programs_root: Path):
    program_path = programs_root / program / "program.yaml"
    return _parse_program(load_yaml_mapping(program_path), program_path)


def _bootstrap_discovery_state_impl(
    program: str,
    *,
    programs_root: Path,
    candidate_store: SourceCandidateStore,
) -> None:
    registry = load_m365_registry(program, programs_root)
    candidate_store.bootstrap_intents(
        workstreams=_load_workstreams(program, programs_root=programs_root),
        registry_artifacts=registry.artifacts,
        as_of=datetime.now(timezone.utc),
    )
