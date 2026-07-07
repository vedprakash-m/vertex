"""WI-2.6: entity-aliases command — surface unresolved entity references.

Usage:
    vertex entity-aliases pending --program <prog>
"""
from __future__ import annotations

from pathlib import Path

import typer

from src.core.config_loader import PROGRAMS_ROOT
from src.core.entity_registry import EntityRegistry
from src.core.program_fact_store import load_program_facts
from src.core.signal_normalizer import collect_unresolved_entity_refs

app = typer.Typer(name="entity-aliases", help="Inspect unresolved entity aliases in a program's fact store.")


@app.command("pending")
def pending(
    program: str = typer.Option(..., "--program", "-p", help="Program ID to inspect."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", hidden=True),
) -> None:
    """List entity_refs in the fact snapshot that cannot be resolved by the entity registry."""
    facts = load_program_facts(program, programs_root=programs_root)
    registry = EntityRegistry.load(program, programs_root=programs_root)
    unresolved = collect_unresolved_entity_refs(facts, registry)

    if not unresolved:
        typer.echo("No unresolved entity_refs found.")
        raise typer.Exit(0)

    typer.echo(f"{len(unresolved)} unresolved entity_ref(s) in program '{program}':")
    for ref in sorted(unresolved):
        typer.echo(f"  - {ref}")
    raise typer.Exit(1)
