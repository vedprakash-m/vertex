"""Gather-run lifecycle configuration and currentness evaluation (D-5/D-24)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from src.core.edition_resolver import PROGRAMS_ROOT, load_program


@dataclass(frozen=True, slots=True)
class GatherRuntimePolicy:
    """Resolved lifecycle behavior, including legacy-safe defaults."""

    run_manifest_mode: Literal["off", "shadow", "enforce"] = "shadow"
    full_discovery_cadence_hours: int = 24
    freshness_warn_hours: int = 30
    freshness_block_hours: int = 48


def load_gather_runtime_policy(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> GatherRuntimePolicy:
    """Load D-24 policy without making older program files behaviorally unsafe."""
    program = load_program(program_id, programs_root=programs_root)
    if program is None:
        return GatherRuntimePolicy()
    gather = program.gather
    return GatherRuntimePolicy(
        run_manifest_mode=gather.run_manifest_mode,
        full_discovery_cadence_hours=gather.full_discovery_cadence_hours,
        freshness_warn_hours=gather.freshness_warn_hours,
        freshness_block_hours=gather.freshness_block_hours,
    )


def freshness_state(
    *,
    last_successful_full_discovery_at: datetime | None,
    now: datetime,
    warn_hours: int,
    block_hours: int,
) -> str:
    """D-5/D-22 currentness classification from immutable timestamps."""
    if last_successful_full_discovery_at is None:
        return "block"
    age_hours = (now - last_successful_full_discovery_at).total_seconds() / 3600.0
    if age_hours >= block_hours:
        return "block"
    if age_hours >= warn_hours:
        return "warn"
    return "current"
