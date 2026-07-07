from __future__ import annotations

import typer


PHASE_MESSAGES: dict[str, str] = {}


def register_phase_stubs(app: typer.Typer) -> None:
    for command_name, message in PHASE_MESSAGES.items():
        app.command(command_name)(_build_stub(message))


def _build_stub(message: str):
    def _command() -> None:
        typer.echo(message)
        raise typer.Exit(code=0)

    return _command