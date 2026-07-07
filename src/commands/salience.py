from __future__ import annotations

import typer

from src.core.feedback.salience_modeler import PROGRAMS_ROOT, load_author_salience, refresh_author_salience, render_author_salience


app = typer.Typer(help="Inspect author salience feedback state.", invoke_without_command=True)


@app.callback(invoke_without_command=True)
def salience_command(
    ctx: typer.Context,
    program: str | None = typer.Option(None, "--program", help="Program id, e.g. myprogram."),
    no_refresh: bool = typer.Option(False, "--no-refresh", help="Read the cached author_salience.yaml without recomputing it."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Compute the salience model but skip writing author_salience.yaml."),
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    if program is None or not program.strip():
        raise typer.BadParameter("--program is required.")
    _show_salience(program.strip(), no_refresh=no_refresh, dry_run=dry_run)
    raise typer.Exit(code=0)


@app.command("show")
def show_salience_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    no_refresh: bool = typer.Option(False, "--no-refresh", help="Read the cached author_salience.yaml without recomputing it."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Compute the salience model but skip writing author_salience.yaml."),
) -> None:
    _show_salience(program.strip(), no_refresh=no_refresh, dry_run=dry_run)
    raise typer.Exit(code=0)


def _show_salience(program_id: str, *, no_refresh: bool, dry_run: bool) -> None:
    if no_refresh:
        model = load_author_salience(program_id, programs_root=PROGRAMS_ROOT)
        if model is None:
            typer.echo(f"No author salience model for {program_id}.")
            return
        typer.echo(render_author_salience(model))
        return

    model, path = refresh_author_salience(
        program_id,
        programs_root=PROGRAMS_ROOT,
        dry_run=dry_run,
    )
    typer.echo(render_author_salience(model))
    if dry_run:
        typer.echo("Dry-run: skipped writing author_salience.yaml.")
    elif path is not None:
        typer.echo(f"Cached model: {path}")