from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import shutil
import subprocess
from typing import Protocol

import typer


REPO_ROOT = Path(__file__).resolve().parents[2]
PROGRAMS_ROOT = REPO_ROOT / "programs"


class CompletedProcessLike(Protocol):
    returncode: int
    stdout: str | None
    stderr: str | None


class CommandRunner(Protocol):
    def __call__(self, command: list[str]) -> CompletedProcessLike:
        ...


@dataclass(frozen=True, slots=True)
class InvestigationArtifacts:
    program_id: str
    mode: str
    target: str
    command: tuple[str, ...]
    output_path: Path


def investigate_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    icm: str | None = typer.Option(None, "--icm", help="Investigate a specific IcM incident id through Geneva Monitoring Agent."),
    account: str | None = typer.Option(None, "--account", help="Run a Geneva health check for the specified Geneva account."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the resolved command and output path without executing it."),
) -> None:
    try:
        artifacts = investigate(
            program_id=program,
            icm_id=icm,
            geneva_account=account,
            dry_run=dry_run,
        )
    except (RuntimeError, ValueError) as error:
        typer.echo(str(error))
        raise typer.Exit(code=2)

    if dry_run:
        typer.echo(f"Resolved command: {' '.join(artifacts.command)}")
        typer.echo(f"Planned output: {artifacts.output_path}")
        raise typer.Exit(code=0)

    typer.echo(f"Investigation complete: {artifacts.output_path}")
    raise typer.Exit(code=0)


def investigate(
    *,
    program_id: str,
    icm_id: str | None = None,
    geneva_account: str | None = None,
    programs_root: Path = PROGRAMS_ROOT,
    runner: CommandRunner | None = None,
    dry_run: bool = False,
    now: datetime | None = None,
) -> InvestigationArtifacts:
    mode, target = _resolve_mode(icm_id=icm_id, geneva_account=geneva_account)
    command = _resolve_investigation_command(mode=mode, target=target)
    timestamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    output_path = programs_root / program_id / "investigations" / f"{timestamp}.{mode}.{target}.md"
    artifacts = InvestigationArtifacts(
        program_id=program_id,
        mode=mode,
        target=target,
        command=tuple(command),
        output_path=output_path,
    )

    if dry_run:
        return artifacts

    completed = (runner or _run_subprocess)(command)
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        raise RuntimeError(stderr or f"Investigation command failed with exit code {completed.returncode}.")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    stdout = (completed.stdout or "").rstrip()
    output_path.write_text(stdout + ("\n" if stdout else ""), encoding="utf-8")
    return artifacts


def _resolve_mode(*, icm_id: str | None, geneva_account: str | None) -> tuple[str, str]:
    if bool(icm_id) == bool(geneva_account):
        raise ValueError("Specify exactly one of --icm or --account.")
    if icm_id is not None:
        target = icm_id.strip()
        if not target:
            raise ValueError("--icm requires a non-empty incident id.")
        return "icm", target
    account_value = (geneva_account or "").strip()
    if not account_value:
        raise ValueError("--account requires a non-empty Geneva account name.")
    return "health", _sanitize_target(account_value)


def _resolve_investigation_command(*, mode: str, target: str) -> list[str]:
    geneva_executable = shutil.which("geneva")
    if geneva_executable is not None:
        if mode == "icm":
            return [geneva_executable, "/investigate", target]
        return [geneva_executable, "/health", target]

    agency_executable = shutil.which("agency")
    if agency_executable is not None:
        prompt = (
            f"Investigate IcM incident {target} and return a markdown incident triage summary."
            if mode == "icm"
            else f"Review Geneva account {target} and return a markdown health summary."
        )
        return [agency_executable, "copilot", "--agent", "geneva-monitoring-agent", "--prompt", prompt]

    raise RuntimeError("Neither `geneva` nor `agency` is available on PATH. Install Geneva Monitoring Agent or Agency CLI first.")


def _run_subprocess(command: list[str]) -> CompletedProcessLike:
    return subprocess.run(  # type: ignore[return-value]
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _sanitize_target(value: str) -> str:
    sanitized = "".join(ch.lower() if ch.isalnum() else "-" for ch in value)
    while "--" in sanitized:
        sanitized = sanitized.replace("--", "-")
    return sanitized.strip("-") or "target"