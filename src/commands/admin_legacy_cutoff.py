from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import typer

from src.core.edition_resolver import PROGRAMS_ROOT, load_program
from src.core.gather_run_manifest import create_legacy_cutoff_manifest, get_legacy_cutoff_at


@dataclass(frozen=True, slots=True)
class LegacyCutoffBootstrapArtifacts:
    program_id: str
    run_id: str
    legacy_cutoff_at: datetime
    already_bootstrapped: bool


def bootstrap_legacy_cutoff_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. armada."),
    at: str | None = typer.Option(
        None,
        "--at",
        help=(
            "ISO-8601 UTC timestamp for the legacy cutoff (§4.17 migration step 5). "
            "Defaults to now. Unstamped signals/facts at or before this timestamp remain "
            "visible once the program later flips gather.run_manifest_mode to 'enforce'; "
            "unstamped records after it are excluded."
        ),
    ),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, hidden=True),
) -> None:
    program_obj = load_program(program, programs_root=programs_root)
    if program_obj is None:
        raise typer.BadParameter(f"Program '{program}' was not found.")

    cutoff_at = _parse_cutoff(at) if at is not None else datetime.now(timezone.utc)
    artifacts = run_bootstrap_legacy_cutoff(
        program_id=program,
        legacy_cutoff_at=cutoff_at,
        programs_root=programs_root,
    )
    if artifacts.already_bootstrapped:
        typer.echo(
            f"Legacy-cutoff manifest already exists for {artifacts.program_id}: "
            f"run_id={artifacts.run_id} | legacy_cutoff_at={artifacts.legacy_cutoff_at.isoformat()} "
            "(no-op; --at is ignored once bootstrapped)."
        )
    else:
        typer.echo(
            f"Bootstrapped legacy-cutoff manifest for {artifacts.program_id}: "
            f"run_id={artifacts.run_id} | legacy_cutoff_at={artifacts.legacy_cutoff_at.isoformat()}"
        )
        typer.echo(
            "Unstamped signals/facts at or before this timestamp remain visible once "
            "gather.run_manifest_mode is flipped to 'enforce' for this program; unstamped "
            "records after it will be excluded."
        )
    raise typer.Exit(code=0)


def run_bootstrap_legacy_cutoff(
    *,
    program_id: str,
    legacy_cutoff_at: datetime,
    programs_root: Path = PROGRAMS_ROOT,
) -> LegacyCutoffBootstrapArtifacts:
    existing_cutoff = get_legacy_cutoff_at(program_id, programs_root=programs_root)
    manifest = create_legacy_cutoff_manifest(
        program_id,
        legacy_cutoff_at=legacy_cutoff_at,
        programs_root=programs_root,
    )
    return LegacyCutoffBootstrapArtifacts(
        program_id=program_id,
        run_id=manifest.run_id,
        legacy_cutoff_at=manifest.legacy_cutoff_at or legacy_cutoff_at,
        already_bootstrapped=existing_cutoff is not None,
    )


def _parse_cutoff(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter(f"--at must be an ISO-8601 timestamp, got {value!r}.") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
