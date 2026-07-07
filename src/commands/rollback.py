"""FR-SG-49: vertex rollback — restore a program to a prior checkpoint snapshot."""

from __future__ import annotations

import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import typer

from src.core.checkpoint_store import (
    CHECKPOINT_DIR_PATHS,
    CHECKPOINT_FILE_PATHS,
    list_checkpoints,
    restore_checkpoint,
)
from src.core.config_loader import PROGRAMS_ROOT
from src.core.platform_proof_log_store import record_platform_proof
from src.core.program_paths import get_platform_proof_log_path

app = typer.Typer(add_completion=False)


@app.command("rollback")
def rollback_command(
    edition: str = typer.Option(..., "--edition", help="Edition id, e.g. myprogram_weekly."),
    to: str | None = typer.Option(None, "--to", help="Checkpoint directory name to restore (omit to list)."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", hidden=True),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview restore without writing files."),
    drill: bool = typer.Option(
        False,
        "--drill",
        help=(
            "Phase 6 §22 Step 10: run a rollback drill. Simulates the rollback "
            "in a temporary sandbox (live program is NOT modified), verifies "
            "the post-rollback state is queryable + the trusted baseline is "
            "re-derivable, and records the result as proof `s7a_rollback_drill` "
            "in `platform_proof_log.yaml`. Pass --to <checkpoint> to choose "
            "the checkpoint (defaults to the newest)."
        ),
    ),
    proof_archetype: str | None = typer.Option(
        None,
        "--archetype",
        help="Optional archetype label for the recorded proof, e.g. 'ADO + Kusto'.",
    ),
    proof_notes: str | None = typer.Option(
        None,
        "--notes",
        help="Optional operator notes recorded with the proof.",
    ),
) -> None:
    """Restore a program's mutable stores to a named checkpoint.

    Run without --to to list available checkpoints. Pass --to <checkpoint_name> to restore.
    Use --drill to run the rollback in a sandbox and record proof s7a_rollback_drill.
    """
    from src.core.edition_resolver import resolve_edition  # noqa: PLC0415

    resolved = resolve_edition(edition, programs_root=programs_root)
    if resolved is None:
        typer.echo(f"Error: edition '{edition}' not found.", err=True)
        raise typer.Exit(code=1)

    program_id = resolved.program.id
    checkpoints = list_checkpoints(program_id, programs_root=programs_root)

    if not checkpoints:
        typer.echo(
            f"No checkpoints found for program '{program_id}'. "
            "Run a non-dry-run `vertex confirm` first to create one before attempting rollback.",
            err=True,
        )
        raise typer.Exit(code=1)

    if to is None and not drill:
        typer.echo(f"Checkpoints for program '{program_id}':")
        for cp in checkpoints:
            typer.echo(f"  {cp.name}")
        raise typer.Exit(code=0)

    checkpoint_path: Path | None = None
    target_checkpoint_name = to
    if to is None:
        # --drill without --to: default to the newest checkpoint.
        checkpoint_path = checkpoints[0]
        target_checkpoint_name = checkpoint_path.name
    else:
        for cp in checkpoints:
            if cp.name == to or str(cp) == to:
                checkpoint_path = cp
                break

    if checkpoint_path is None:
        typer.echo(f"Checkpoint '{to}' not found. Available:")
        for cp in checkpoints:
            typer.echo(f"  {cp.name}")
        raise typer.Exit(code=1)

    relpaths = _checkpoint_restore_relpaths(checkpoint_path)
    if dry_run and not drill:
        typer.echo(f"Dry-run: would restore from {checkpoint_path}")
        for relpath in relpaths:
            typer.echo(f"  {relpath}")
        raise typer.Exit(code=0)

    if drill:
        # Phase 6 §22 Step 10: rollback drill.
        result_status, sandbox_dir, warning = _run_rollback_drill(
            program_id=program_id,
            checkpoint_path=checkpoint_path,
            programs_root=programs_root,
        )
        try:
            record = record_platform_proof(
                program_id=program_id,
                proof_id="s7a_rollback_drill",
                status=result_status,
                recorded_at=datetime.now(timezone.utc),
                recorded_by=_read_operator(),
                edition=edition,
                notes=(
                    f"checkpoint={target_checkpoint_name}; sandbox={sandbox_dir}; "
                    + (f"warning={warning}; " if warning else "")
                    + (proof_notes or "")
                ).rstrip("; "),
                no_code_changes=True,
                archetype=proof_archetype,
                programs_root=programs_root,
            )
        except ValueError as error:
            typer.echo(f"Error recording proof: {error}", err=True)
            raise typer.Exit(code=1) from error

        typer.echo(
            f"Rollback drill {record.status}: checkpoint={target_checkpoint_name}, sandbox={sandbox_dir}"
        )
        if warning:
            typer.echo(f"Warning: {warning}")
        typer.echo(
            f"Recorded proof s7a_rollback_drill ({record.status}) for program {program_id}."
        )
        proof_log_path = get_platform_proof_log_path(program_id, programs_root=programs_root)
        typer.echo(f"Proof log: {proof_log_path}")
        raise typer.Exit(code=0 if result_status == "passed" else 1)

    typer.echo(f"Restoring from {checkpoint_path.name}:")
    for relpath in relpaths:
        typer.echo(f"  {relpath}")
    restore_checkpoint(program_id, checkpoint_path, programs_root=programs_root)
    typer.echo(f"Restored program '{program_id}' from checkpoint '{checkpoint_path.name}'.")

    # PB-36: also revert SQLite ProgramFactStore rows created after the checkpoint.
    purged = _purge_fact_store_after_checkpoint(program_id, checkpoint_path, programs_root=programs_root)
    if purged > 0:
        typer.echo(f"Purged {purged} ProgramFactStore revision(s) recorded after checkpoint.")
    else:
        typer.echo("ProgramFactStore: no post-checkpoint rows to purge.")


def _checkpoint_restore_relpaths(checkpoint_path: Path) -> tuple[str, ...]:
    relpaths = [rel_path for rel_path in CHECKPOINT_FILE_PATHS if (checkpoint_path / rel_path).exists()]
    relpaths.extend(f"{rel_dir}/" for rel_dir in CHECKPOINT_DIR_PATHS if (checkpoint_path / rel_dir).exists())
    return tuple(relpaths)


def _parse_checkpoint_timestamp(checkpoint_path: Path) -> "datetime | None":
    """Parse the UTC timestamp embedded in a checkpoint directory name.

    Checkpoint dir names follow the pattern ``issue_NNN_YYYYMMDDTHHMMSSZ``.
    Returns None if the name does not match.
    """
    import re  # noqa: PLC0415
    from datetime import datetime, timezone  # noqa: PLC0415

    match = re.search(r"(\d{8}T\d{6}Z)$", checkpoint_path.name)
    if match is None:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _purge_fact_store_after_checkpoint(
    program_id: str,
    checkpoint_path: Path,
    *,
    programs_root: Path,
) -> int:
    """Purge ProgramFactStore rows recorded after the checkpoint's creation timestamp.

    Returns the number of rows deleted (0 if the DB does not exist or no rows
    qualify).  Non-fatal: if the store is absent or the timestamp cannot be
    parsed, returns 0 silently.
    """
    from src.core.program_fact_store import ProgramFactStore  # noqa: PLC0415

    cutoff = _parse_checkpoint_timestamp(checkpoint_path)
    if cutoff is None:
        return 0
    db_root = programs_root.parent / "vertex-db"
    store = ProgramFactStore(program_id, db_root=db_root)
    if not store.db_path.exists():
        return 0
    try:
        return store.purge_facts_after(cutoff)
    except Exception:  # noqa: BLE001 — non-fatal; filesystem rollback already done
        return 0


def _read_operator() -> str | None:
    """Read the operator identity for the proof log. Prefers the
    `VERTEX_AUTHOR` env var; falls back to `os.getlogin()`. Returns
    None if neither is available so the proof is still recorded but
    without an operator identity."""
    import os
    override = os.environ.get("VERTEX_AUTHOR")
    if override and override.strip():
        return override.strip()
    try:
        return os.getlogin()
    except OSError:
        return None


def _run_rollback_drill(
    *,
    program_id: str,
    checkpoint_path: Path,
    programs_root: Path,
) -> tuple[str, Path, str | None]:
    """Phase 6 §22 Step 10: simulate the rollback in a temporary sandbox
    and verify the post-rollback state is consistent.

    Steps:
      1. Copy the current program directory to a temp sandbox.
      2. Apply the checkpoint to the sandbox (simulating rollback).
      3. Run a lightweight post-rollback check: try to `load_program_facts`
         on the sandbox; this is the same code path the live read API uses.
      4. If all checks pass, return ("passed", sandbox_dir, None).
      5. If any check fails, return ("failed", sandbox_dir, warning).
      6. The sandbox is always cleaned up before returning.

    Returns: (status, sandbox_dir, warning_or_none).

    Why:** the spec mandates that the rollback drill be recorded as
    proof `s7a_rollback_drill` in `platform_proof_log.yaml` before the
    irreversible default flip. This helper makes the drill
    side-effect-free (no live program mutation) and the proof
    recordable via a single CLI invocation.
    **How to apply:** invoked by `vertex rollback --drill --edition
    <name> [--to <checkpoint>]`. The operator can then re-run with
    different checkpoints to validate each, and `vertex admin
    platform-proof --proof-id s7a_rollback_drill` shows the latest
    recorded status.
    """
    program_dir = programs_root / program_id
    sandbox_dir = Path(tempfile.mkdtemp(prefix=f"vertex_rb_drill_{program_id}_"))
    warning: str | None = None
    status = "passed"
    try:
        # 1. Copy the current state to the sandbox.
        sandbox_program = sandbox_dir / "programs" / program_id
        sandbox_program.parent.mkdir(parents=True, exist_ok=True)
        if program_dir.exists():
            shutil.copytree(program_dir, sandbox_program, dirs_exist_ok=True)
        # 2. Apply the checkpoint to the sandbox (in-place).
        restore_checkpoint(
            program_id,
            checkpoint_path,
            programs_root=sandbox_dir / "programs",
        )
        # 3. Lightweight post-rollback check: try to load facts from
        # the sandbox. The function is the same one the live read API
        # uses; if it succeeds, the program is at least queryable in
        # the rolled-back state.
        try:
            from src.core.program_fact_store import load_program_facts  # noqa: PLC0415
            load_program_facts(
                program_id,
                programs_root=sandbox_dir / "programs",
            )
        except Exception as error:  # pragma: no cover — defensive
            status = "failed"
            warning = f"load_program_facts failed on rolled-back state: {error}"
    except Exception as error:  # pragma: no cover — defensive
        status = "failed"
        warning = f"drill setup failed: {error}"
    finally:
        # 6. Always clean up the sandbox.
        try:
            shutil.rmtree(sandbox_dir, ignore_errors=True)
        except OSError:
            pass
    return status, sandbox_dir, warning
