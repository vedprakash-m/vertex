from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import typer

from src.commands.reality import (
    _build_delivery_date_snapshot_provider,
    _build_metric_definition_map,
    _load_expected_gather_cadence_hours,
)
from src.core.reality_reconciler import reconcile_reality
from src.core.reality_store import RealityStore


def reconcile_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    tier: str = typer.Option("all", "--tier", help="One of: hot, warm, cold, all."),
    format: str = typer.Option("text", "--format", help="Output format: text or json."),
    db_root: Path | None = typer.Option(None, hidden=True),
) -> None:
    normalized_program = program.strip()
    if not normalized_program:
        raise typer.BadParameter("--program must be non-empty")

    normalized_tier = _normalize_tier(tier)
    normalized_format = _normalize_format(format)
    as_of = datetime.now(timezone.utc)

    store = RealityStore(normalized_program, db_root=db_root)
    store.initialize()
    result = reconcile_reality(
        store=store,
        as_of=as_of,
        l1_observations_written=0,
        delivery_date_snapshot_provider=_build_delivery_date_snapshot_provider(normalized_program),
        metric_definitions_by_id=_build_metric_definition_map(as_of=as_of),
        expected_gather_cadence_hours=_load_expected_gather_cadence_hours(normalized_program),
        included_metric_tiers=None if normalized_tier == "all" else frozenset({normalized_tier}),
        write_digest_cache=normalized_tier == "all",
    )
    payload = {
        "program_id": normalized_program,
        "tier": normalized_tier,
        "digest_cache_updated": normalized_tier == "all",
        "evaluations_written": result.evaluations_written,
        "challenges_opened": result.challenges_opened,
        "hypotheses_challenged": result.hypotheses_challenged,
        "health": result.digest.health,
        "as_of": result.as_of.isoformat(),
    }
    _emit(payload, format=normalized_format)
    raise typer.Exit(code=0)


def _normalize_tier(value: str) -> str:
    normalized = value.strip().lower()
    if normalized not in {"hot", "warm", "cold", "all"}:
        raise typer.BadParameter("--tier must be one of: hot, warm, cold, all")
    return normalized


def _normalize_format(value: str) -> str:
    normalized = value.strip().lower()
    if normalized not in {"text", "json"}:
        raise typer.BadParameter("--format must be one of: text, json")
    return normalized


def _emit(payload: dict[str, object], *, format: str) -> None:
    if format == "json":
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return

    typer.echo(f"Reality reconcile complete: {payload['program_id']} (tier: {payload['tier']})")
    typer.echo(f"Health: {payload['health']}")
    typer.echo(f"Evaluations written: {payload['evaluations_written']}")
    typer.echo(f"Challenges opened: {payload['challenges_opened']}")
    typer.echo(f"Hypotheses challenged: {payload['hypotheses_challenged']}")
    typer.echo(
        "Digest cache: updated"
        if payload["digest_cache_updated"]
        else "Digest cache: not updated (tier-filtered pass)"
    )