from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import typer

from src.core.action_tracker import get_actions_path
from src.core.assumption_tracker import get_assumptions_path
from src.core.claim_tracker import get_claims_path
from src.core.edition_resolver import EDITIONS_ROOT, PROGRAMS_ROOT, list_editions_for_program, load_program
from src.core.snapshot_store import ARCHIVE_ROOT
from src.core.decision_register import get_decisions_path
from src.core.dependency_graph import get_dependencies_path
from src.core.milestone_engine import get_milestones_path
from src.core.program_fact_store import build_legacy_program_fact_snapshot, persist_program_fact_snapshot
from src.core.reality_store import get_program_reality_db_path
from src.core.risk_register_engine import get_risk_register_path
from src.core.workstream_documents import get_workstreams_path


SUPPORTED_LEGACY_FACT_TYPES = (
    "action.item",
    "claim.entry",
    "claim.status_update",
    "decision.ask",
    "baseline.trust_event",
    "skip.issue",
    "assumption.entry",
    "decision.entry",
    "risk.entry",
    "dependency.link",
    "milestone.entry",
    "workstream.entry",
)


@dataclass(frozen=True, slots=True)
class LegacyStateMigrationArtifacts:
    program_id: str
    storage_backend: str
    source_inventory: tuple[str, ...]
    fact_count: int
    created_count: int
    noop_count: int
    superseded_count: int
    proposed_revision_count: int
    database_path: Path
    dry_run: bool


def migrate_legacy_state_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview imported fact counts without writing to the fact store."),
    editions_root: Path = typer.Option(EDITIONS_ROOT, hidden=True),
    archive_root: Path = typer.Option(ARCHIVE_ROOT, hidden=True),
    db_root: Path | None = typer.Option(None, hidden=True),
) -> None:
    artifacts = run_migrate_legacy_state(
        program_id=program,
        programs_root=PROGRAMS_ROOT,
        editions_root=editions_root,
        archive_root=archive_root,
        dry_run=dry_run,
        db_root=db_root,
    )
    typer.echo(
        f"Migrated legacy state for {artifacts.program_id} | storage_backend={artifacts.storage_backend} | "
        f"facts={artifacts.fact_count} | created={artifacts.created_count} | noop={artifacts.noop_count} | "
        f"superseded={artifacts.superseded_count} | proposed={artifacts.proposed_revision_count}"
    )
    typer.echo(f"Source inventory: {', '.join(artifacts.source_inventory)}")
    if artifacts.dry_run:
        typer.echo("Dry-run: fact store was not modified.")
    else:
        typer.echo(f"Fact store: {artifacts.database_path}")
    raise typer.Exit(code=0)


def run_migrate_legacy_state(
    *,
    program_id: str,
    programs_root: Path = PROGRAMS_ROOT,
    editions_root: Path = EDITIONS_ROOT,
    archive_root: Path = ARCHIVE_ROOT,
    dry_run: bool = False,
    db_root: Path | None = None,
) -> LegacyStateMigrationArtifacts:
    program = load_program(program_id, programs_root=programs_root)
    if program is None:
        raise typer.BadParameter(f"Program '{program_id}' was not found.")

    source_inventory = _source_inventory(
        program_id,
        programs_root=programs_root,
        editions_root=editions_root,
        archive_root=archive_root,
    )
    recorded_at = datetime.now(timezone.utc)
    snapshot = build_legacy_program_fact_snapshot(
        program_id,
        recorded_at=recorded_at,
        fact_types=SUPPORTED_LEGACY_FACT_TYPES,
        programs_root=programs_root,
        editions_root=editions_root,
        archive_root=archive_root,
    )
    database_path = get_program_reality_db_path(program_id, db_root=db_root or programs_root.parent)
    if dry_run:
        return LegacyStateMigrationArtifacts(
            program_id=program_id,
            storage_backend=program.storage_backend,
            source_inventory=source_inventory,
            fact_count=len(snapshot.facts),
            created_count=0,
            noop_count=0,
            superseded_count=0,
            proposed_revision_count=0,
            database_path=database_path,
            dry_run=True,
        )

    results = persist_program_fact_snapshot(
        snapshot,
        recorded_at=recorded_at,
        accepted_by="vertex.admin.migrate-legacy-state",
        db_root=db_root or programs_root.parent,
    )
    return LegacyStateMigrationArtifacts(
        program_id=program_id,
        storage_backend=program.storage_backend,
        source_inventory=source_inventory,
        fact_count=len(snapshot.facts),
        created_count=sum(1 for result in results if result.action == "created"),
        noop_count=sum(1 for result in results if result.action == "noop"),
        superseded_count=sum(1 for result in results if result.action == "superseded"),
        proposed_revision_count=sum(1 for result in results if result.action == "proposed_revision"),
        database_path=database_path,
        dry_run=False,
    )


def _source_inventory(
    program_id: str,
    *,
    programs_root: Path,
    editions_root: Path,
    archive_root: Path,
) -> tuple[str, ...]:
    baseline_path = programs_root / program_id / "trusted_baseline.yaml"
    archive_index_paths = tuple(
        str(archive_root / edition_id / "index.json")
        for edition_id in list_editions_for_program(program_id, editions_root=editions_root)
    )
    return (
        str(get_actions_path(program_id, programs_root)),
        str(get_claims_path(program_id, programs_root)),
        str(baseline_path),
        *archive_index_paths,
        str(get_assumptions_path(program_id, programs_root)),
        str(get_decisions_path(program_id, programs_root)),
        str(get_risk_register_path(program_id, programs_root)),
        str(get_dependencies_path(program_id, programs_root)),
        str(get_milestones_path(program_id, programs_root)),
        str(get_workstreams_path(program_id, programs_root)),
    )