"""FR-SG-48: vertex connectors — poll external (non-ADO) dependency connectors."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from src.core.config_loader import PROGRAMS_ROOT
from src.core.connector_polling import poll_and_save_external_connectors
from src.core.slice_contract_loader import load_external_connector_configs

app = typer.Typer(help="External connector management (FR-SG-48).")


@app.command("poll")
def poll_connectors(
    program: str = typer.Option(..., "--program", help="Program ID to poll connectors for."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Poll but do not persist results."),
    programs_root: Optional[Path] = typer.Option(None, "--programs-root", hidden=True),
) -> None:
    """Poll all external connectors configured in programs/{program}/slice_contracts.yaml.

    Each connector entry must specify connector_type, dep_id, source_url, and team.
    Results are persisted to programs/{program}/external_dependencies.jsonl unless --dry-run.
    """
    resolved_root = programs_root or PROGRAMS_ROOT
    contracts_path = resolved_root / program / "slice_contracts.yaml"
    configs = load_external_connector_configs(contracts_path)

    if not configs:
        typer.echo(f"No external_connectors configured for program '{program}'.")
        raise typer.Exit(code=0)

    typer.echo(f"Polling {len(configs)} external connector(s) for program '{program}'...")
    if dry_run:
        typer.echo("(dry-run: results will not be persisted)")
        for cfg in configs:
            typer.echo(f"  • {cfg.dep_id} [{cfg.connector_type}] → {cfg.source_url}")
        raise typer.Exit(code=0)

    results = poll_and_save_external_connectors(program, configs, programs_root=resolved_root)
    if results:
        typer.echo(f"Polled and saved {len(results)} external dependency record(s):")
        for dep in results:
            typer.echo(f"  ✓ {dep.dep_id} team={dep.team} items={dep.tracked_items}")
    else:
        typer.echo("No connectors returned results (check logs for errors).")
