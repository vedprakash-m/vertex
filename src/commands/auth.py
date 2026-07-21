from __future__ import annotations

import shutil
import subprocess

import typer

from src.core.ado_scheduled_credential import get_scheduled_ado_pat, set_scheduled_ado_pat


app = typer.Typer(help="Authentication setup commands.")

_DEVICE_LOGIN_URL = "https://aka.ms/devicelogin"


@app.command("armada-scheduled-pat")
def armada_scheduled_pat_command(
    status: bool = typer.Option(False, "--status", help="Check whether the scheduled Armada PAT is configured."),
) -> None:
    """Configure the scheduler-only ADO PAT without exposing it in shell history.

    This command intentionally has no value-taking option: PATs must never
    appear in a command line, task XML, or process argument list.
    """
    if status:
        try:
            get_scheduled_ado_pat()
        except RuntimeError as exc:
            typer.echo(f"Armada scheduled ADO PAT: not configured ({exc})", err=True)
            raise typer.Exit(code=2) from exc
        typer.echo("Armada scheduled ADO PAT: configured (Credential Manager).")
        raise typer.Exit(code=0)

    secret = typer.prompt("Read-only ADO PAT for scheduled Armada gather", hide_input=True, confirmation_prompt=True)
    try:
        set_scheduled_ado_pat(secret)
    except (RuntimeError, ValueError) as exc:
        typer.echo(f"Could not store scheduled ADO PAT: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo("Armada scheduled ADO PAT stored in Credential Manager.")
    raise typer.Exit(code=0)


@app.command("setup")
def setup_command(
    tenant_id: str | None = typer.Option(None, "--tenant-id", help="Optional Entra tenant id passed to Azure CLI sign-in."),
    use_device_code: bool = typer.Option(False, "--use-device-code", help="Use Azure CLI device-code login instead of the default browser flow."),
) -> None:
    if not _has_azure_cli():
        typer.echo("Azure CLI is not installed or not on PATH. Install Azure CLI, then re-run `vertex admin auth setup`.", err=True)
        raise typer.Exit(code=2)

    command = ["az", "login"]
    if tenant_id is not None and tenant_id.strip():
        command.extend(["--tenant", tenant_id.strip()])
    if use_device_code:
        command.append("--use-device-code")

    typer.echo("Starting Azure CLI sign-in...")
    exit_code = _run_azure_cli_login(command)
    if exit_code != 0:
        typer.echo(
            "Azure CLI sign-in did not complete. If browser-based login is blocked by conditional access, "
            f"open {_DEVICE_LOGIN_URL} and re-run `vertex admin auth setup --use-device-code`.",
            err=True,
        )
        raise typer.Exit(code=2)

    typer.echo("Azure CLI sign-in completed.")
    typer.echo("Next: run `vertex doctor --check-auth` to verify ADO, Graph, Agency CLI, and Kusto readiness.")
    raise typer.Exit(code=0)


def _has_azure_cli() -> bool:
    return shutil.which("az") is not None


def _run_azure_cli_login(command: list[str]) -> int:
    completed = subprocess.run(command, check=False)
    return int(completed.returncode)
