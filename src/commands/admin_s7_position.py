from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess

import typer

from src.core.config_loader import PROGRAMS_ROOT
from src.core.platform_s7_store import (
    get_platform_s7_state_path,
    load_platform_s7_state,
    save_platform_s7_state,
)


def admin_s7_position_command(
    position: str = typer.Option(..., "--position", help="S7 readiness position: complete or deferred."),
    justification: str | None = typer.Option(None, "--justification", help="Required when position is deferred."),
    programs_root: str | None = typer.Option(None, hidden=True),
) -> None:
    resolved_programs_root = Path(programs_root) if programs_root is not None else PROGRAMS_ROOT
    normalized_position = position.strip().lower()
    normalized_justification = (justification or "").strip() or None

    if normalized_position not in {"complete", "deferred"}:
        raise typer.BadParameter("--position must be 'complete' or 'deferred'.")
    if normalized_position == "deferred" and normalized_justification is None:
        raise typer.BadParameter("--justification is required when --position deferred is set.")
    if normalized_position == "complete" and normalized_justification is not None:
        raise typer.BadParameter("--justification is only allowed when --position deferred is set.")

    current = load_platform_s7_state(programs_root=resolved_programs_root)
    if current is not None and current.position == "complete" and normalized_position == "deferred":
        raise typer.BadParameter(
            "Cannot revert a complete S7 position to deferred through the supported admin path."
        )

    try:
        record = save_platform_s7_state(
            position=normalized_position,
            recorded_at=datetime.now(timezone.utc),
            recorded_by=_read_operator(),
            justification=normalized_justification,
            programs_root=resolved_programs_root,
        )
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error

    typer.echo(f"Recorded S7 platform position: {record.position}.")
    typer.echo(f"Path: {get_platform_s7_state_path(programs_root=resolved_programs_root)}")


def _read_operator() -> str | None:
    override = os.environ.get("VERTEX_AUTHOR")
    if override and override.strip():
        return override.strip()
    try:
        completed = subprocess.run(
            ["git", "config", "user.name"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        completed = None
    if completed is not None and completed.returncode == 0:
        candidate = completed.stdout.strip()
        if candidate:
            return candidate
    username = os.environ.get("USERNAME")
    if username and username.strip():
        return username.strip()
    return None
