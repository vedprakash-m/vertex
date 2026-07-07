"""GAP-40: ``vertex admin upgrade-state`` CLI wrapper.

The schema-evolution engine lives in ``src/core/schema_evolution.py`` and
walks a ``program.yaml`` document through the ``PROGRAM_YAML_EVOLUTION``
chain (2.0 → 3.0 → 4.0). This module is the thin Typer command that loads the
document, runs the engine, prints the planned/applied steps, and persists
both the rewritten ``program.yaml`` and one ``migration_log.jsonl`` row per
applied step. It mirrors the structure of ``admin_fact_store_migrate.py``:
a public ``upgrade_state_command`` (Typer entrypoint) delegating to a
testable ``run_upgrade_state`` helper.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer
import yaml

from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.migration_log import migration_log_path
from src.core.schema_evolution import (
    PROGRAM_YAML_EVOLUTION,
    SchemaEvolutionResult,
    run_evolution,
)
from src.core.yaml_utils import load_yaml_mapping


@dataclass(frozen=True, slots=True)
class UpgradeStateArtifacts:
    """Audit-friendly summary returned by ``run_upgrade_state``."""

    program_id: str
    program_path: Path
    migration_log_path: Path
    starting_version: str
    ending_version: str
    steps_applied: int
    applied: bool


def upgrade_state_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview the evolution steps without writing program.yaml or migration_log.jsonl."),
    apply: bool = typer.Option(False, "--apply", help="Apply the evolution: rewrite program.yaml and append migration_log.jsonl rows. Mutually exclusive with --dry-run."),
    operator: str = typer.Option("vertex.admin", "--operator", help="Operator identity recorded in the migration log."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, hidden=True),
) -> None:
    if dry_run and apply:
        raise typer.BadParameter("Use either --dry-run or --apply, not both.")
    # Match the sibling `migrate-legacy-state` default: no flag means apply.
    do_apply = apply or not dry_run

    artifacts = run_upgrade_state(
        program_id=program,
        programs_root=programs_root,
        apply=do_apply,
        operator=operator,
    )

    typer.echo(
        f"Schema upgrade for {artifacts.program_id} | "
        f"{artifacts.starting_version} → {artifacts.ending_version} | "
        f"steps={artifacts.steps_applied} | "
        f"mode={'apply' if artifacts.applied else 'dry-run'}"
    )
    if artifacts.steps_applied == 0:
        typer.echo("No evolution steps required (already at target version).")
    if artifacts.applied:
        typer.echo(f"Program rewritten: {artifacts.program_path}")
        typer.echo(f"Migration log: {artifacts.migration_log_path}")
    else:
        typer.echo("Dry-run: program.yaml and migration_log.jsonl were not modified.")
    raise typer.Exit(code=0)


def run_upgrade_state(
    *,
    program_id: str,
    programs_root: Path = PROGRAMS_ROOT,
    apply: bool = False,
    operator: str = "vertex.admin",
) -> UpgradeStateArtifacts:
    """Run the ``program.yaml`` schema evolution.

    Loads ``programs/<program_id>/program.yaml`` as a dict, walks
    ``PROGRAM_YAML_EVOLUTION`` via ``run_evolution``, and (when ``apply``)
    writes the transformed document back to disk. The engine appends one
    ``migration_log.jsonl`` row per applied step itself (file-locked +
    fsync, per PB-37), so this helper only needs to pass the canonical
    migration-log path through.
    """
    program_path = programs_root / program_id / "program.yaml"
    if not program_path.exists():
        raise typer.BadParameter(f"Program '{program_id}' was not found at {program_path}.")

    document: dict[str, Any] = load_yaml_mapping(program_path)
    log_path = migration_log_path(program_id, programs_root)

    result: SchemaEvolutionResult = run_evolution(
        document,
        artifact="program_yaml",
        evolution=PROGRAM_YAML_EVOLUTION,
        apply=apply,
        migration_log_path=log_path if apply else None,
        operator=operator,
    )

    if apply and result.steps_applied:
        # The engine mutates ``document`` in place when applying; persist the
        # rewritten document back to program.yaml so the bump survives.
        with program_path.open("w", encoding="utf-8") as fh:
            yaml.safe_dump(document, fh, sort_keys=False, allow_unicode=True)

    return UpgradeStateArtifacts(
        program_id=program_id,
        program_path=program_path,
        migration_log_path=log_path,
        starting_version=result.starting_version,
        ending_version=result.ending_version,
        steps_applied=len(result.steps_applied),
        applied=result.applied,
    )