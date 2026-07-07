from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path

import typer

from src.core.config_loader import EDITIONS_ROOT, PROGRAMS_ROOT
from src.core.platform_proof_catalog import (
    iter_platform_required_proof_definitions,
    validate_platform_proof_identity,
)
from src.core.platform_proof_log_store import (
    get_platform_proof_log_path,
    load_platform_proof_records,
    record_platform_proof,
    resolve_platform_proof_program,
)


def admin_platform_proof_command(
    proof_id: str | None = typer.Option(None, "--proof-id", help="Proof identifier, for example p4a_clean_machine or p6b_ado_only."),
    program: str | None = typer.Option(None, "--program", help="Program id that owns the proof log."),
    edition: str | None = typer.Option(None, "--edition", help="Edition name; used to infer the owning program and stored edition when omitted."),
    status: str = typer.Option("passed", "--status", help="Proof outcome: passed or failed."),
    notes: str | None = typer.Option(None, "--notes", help="Optional operator notes describing the proof run."),
    elapsed_minutes: float | None = typer.Option(None, "--elapsed-minutes", min=0.0, help="Optional elapsed time for the proof run."),
    no_code_changes: bool | None = typer.Option(None, "--no-code-changes/--code-changes", help="Whether the proof run completed without editing code."),
    confirm_exit_code: int | None = typer.Option(None, "--confirm-exit-code", min=0, help="Optional confirm exit code recorded during the proof run."),
    archetype: str | None = typer.Option(None, "--archetype", help="Optional archetype label, for example 'ADO-only' or 'ADO + M365'."),
    plan: bool = typer.Option(False, "--plan", help="Show required platform proofs and repo coverage instead of recording a new proof."),
    editions_root: str | None = typer.Option(None, hidden=True),
    programs_root: str | None = typer.Option(None, hidden=True),
) -> None:
    resolved_editions_root = Path(editions_root) if editions_root is not None else EDITIONS_ROOT
    resolved_programs_root = Path(programs_root) if programs_root is not None else PROGRAMS_ROOT
    try:
        program_id, resolved_edition = resolve_platform_proof_program(
            edition=edition,
            program=program,
            editions_root=resolved_editions_root,
            programs_root=resolved_programs_root,
        )
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error

    if plan:
        if proof_id is not None:
            raise typer.BadParameter("--proof-id cannot be combined with --plan.")
        typer.echo(_render_platform_proof_plan(program_id=program_id, programs_root=resolved_programs_root))
        return

    normalized_proof_id = (proof_id or "").strip()
    if not normalized_proof_id:
        raise typer.BadParameter("--proof-id is required unless --plan is used.")
    try:
        _, normalized_archetype = validate_platform_proof_identity(
            proof_id=normalized_proof_id,
            archetype=archetype,
        )
        record = record_platform_proof(
            program_id=program_id,
            proof_id=normalized_proof_id,
            status=status,
            recorded_at=datetime.now(timezone.utc),
            recorded_by=_read_operator(),
            edition=resolved_edition,
            notes=notes,
            elapsed_minutes=elapsed_minutes,
            no_code_changes=no_code_changes,
            confirm_exit_code=confirm_exit_code,
            archetype=normalized_archetype,
            programs_root=resolved_programs_root,
        )
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error

    path = get_platform_proof_log_path(program_id, programs_root=resolved_programs_root)
    typer.echo(
        f"Recorded platform proof {record.proof_id} ({record.status}) for program {program_id}."
    )
    typer.echo(f"Path: {path}")


def _render_platform_proof_plan(*, program_id: str, programs_root: Path) -> str:
    records = load_platform_proof_records(program_id, programs_root=programs_root)
    lines = [f"Platform proof plan for program {program_id}:"]
    if not records:
        lines.append("Current log: no proof records recorded yet.")
    else:
        lines.append(f"Current log: {len(records)} recorded entr{'y' if len(records) == 1 else 'ies'}.")
    for definition in iter_platform_required_proof_definitions():
        matching_records = [record for record in records if record.proof_id == definition.proof_id]
        latest_record = matching_records[-1] if matching_records else None
        current_status = latest_record.status if latest_record is not None else "missing"
        suffix = ""
        if definition.archetype is not None:
            suffix = f" | archetype={definition.archetype}"
        if latest_record is not None:
            lines.append(
                f"- {definition.proof_id} | {current_status} | phase={definition.phase}{suffix} | {definition.description}"
            )
        else:
            lines.append(
                f"- {definition.proof_id} | missing | phase={definition.phase}{suffix} | {definition.description}"
            )
    return "\n".join(lines)


def _read_operator() -> str | None:
    override = os.environ.get("VERTEX_AUTHOR")
    if override and override.strip():
        return override.strip()
    try:
        return os.getlogin()
    except OSError:
        return None
