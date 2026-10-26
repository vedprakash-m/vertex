"""Gather-run lifecycle configuration and currentness evaluation (D-5/D-24)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Literal

from src.commands.gather_pipeline.models import GatherArtifacts
from src.core.edition_resolver import PROGRAMS_ROOT, load_program
from src.core.exceptions import StateError
from src.core.gather_run_manifest import GatherRunManifest


def semantic_gather_exit_code(
    artifacts: GatherArtifacts, *, required_scope_degradation_fn: Callable[[GatherArtifacts], bool],
) -> int:
    """D-7's successful-process outcome mapping.

    The ADO channel defines required delivery scope; all other currently
    optional integrations may degrade without invalidating the manual refresh.
    Framework exceptions deliberately remain exceptions (the scheduler treats
    their conventional non-zero exit as failed/non-current per D-7).
    """
    if required_scope_degradation_fn(artifacts):
        return 3
    if not artifacts.integration_errors:
        return 0
    return 2


def has_required_ado_degradation(artifacts: GatherArtifacts) -> bool:
    """ADO is the authoritative delivery-scope channel.

    Treat every ADO integration error as required-scope degradation. A
    stage-name allowlist would inevitably miss a newly introduced ADO stage
    and silently promote a partial run to FULL.
    """
    return any(
        error.source == "ado"
        for error in artifacts.integration_errors
    )


def has_required_scope_degradation(
    artifacts: GatherArtifacts, *, required_ado_degradation_fn: Callable[[GatherArtifacts], bool],
) -> bool:
    """Required scope is degraded by ADO failure, incomplete capture, or skew."""
    capture_times = tuple(result.captured_at for result in artifacts.ado_query_results)
    capture_skew_exceeded = (
        len(capture_times) >= 2
        and (max(capture_times) - min(capture_times)).total_seconds() > 300
    )
    query_capture_incomplete = any(
        result.cap_reached
        or result.completeness_state != "FULL"
        or result.failure_category is not None
        for result in artifacts.ado_query_results
    )
    return required_ado_degradation_fn(artifacts) or query_capture_incomplete or capture_skew_exceeded


def alert_ledger_state(
    program_id: str, *, programs_root: Path, read_alerts_fn: Callable[..., Any],
) -> tuple[dict[str, tuple[int, datetime | None, datetime | None]], bool]:
    """Read the alert ledger without allowing observability I/O to block gather.

    The boolean is intentionally carried to the manifest: failure to observe
    alert lifecycle evidence is operationally meaningful, but it must not
    turn a successfully gathered authoritative scope into a failed run.
    """
    try:
        return (
            {
                record.alert_id: (record.occurrence_count, record.last_seen, record.resolved_at)
                for record in read_alerts_fn(program_id, programs_root=programs_root, include_resolved=True)
            },
            False,
        )
    except (OSError, StateError, ValueError):
        return {}, True


def full_discovery_timestamp(manifest: GatherRunManifest | None) -> datetime | None:
    if manifest is None:
        return None
    return manifest.last_successful_full_discovery_at or manifest.last_query_captured_at or manifest.finished_at


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
