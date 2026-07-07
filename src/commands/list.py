from __future__ import annotations

import csv
from io import StringIO
import json

import typer

from src.core.config_loader import discover_report_editions, load_bundle


app = typer.Typer(help="List configured Vertex resources.")


@app.command("editions")
def list_editions(
    format: str = typer.Option("human", "--format", help="Output format: human, json, or csv."),
) -> None:
    editions = discover_report_editions()
    if not editions:
        typer.echo("No report editions found.")
        raise typer.Exit(code=1)

    rows: list[dict[str, object]] = []
    for edition in editions:
        bundle = load_bundle(edition)
        rows.append(
            {
                "name": bundle.config.edition.name,
                "type": bundle.config.edition.type,
                "cadence": bundle.config.edition.cadence,
            }
        )
    _emit_list_output(rows, format=format, columns=("name", "type", "cadence"))


@app.command("workstreams")
def list_workstreams(
    edition: str = typer.Option(..., "--edition", help="Edition name, e.g. myprogram_weekly."),
    format: str = typer.Option("human", "--format", help="Output format: human, json, or csv."),
) -> None:
    bundle = load_bundle(edition)
    if bundle.program_context is None or not bundle.program_context.workstreams:
        typer.echo(f"No workstreams configured for {edition}.")
        raise typer.Exit(code=1)

    rows: list[dict[str, object]] = []
    for workstream in bundle.program_context.workstreams:
        rows.append(
            {
                "name": workstream.name,
                "dri_email": workstream.dri_email or "unassigned",
                "area_paths": tuple(workstream.area_paths),
            }
        )
    _emit_list_output(rows, format=format, columns=("name", "dri_email", "area_paths"))


@app.command("dris")
def list_dris(
    edition: str = typer.Option(..., "--edition", help="Edition name, e.g. myprogram_weekly."),
    format: str = typer.Option("human", "--format", help="Output format: human, json, or csv."),
) -> None:
    bundle = load_bundle(edition)
    if bundle.program_context is None:
        typer.echo(f"No program context configured for {edition}.")
        raise typer.Exit(code=1)

    rows: list[dict[str, object]] = []
    displayed = False
    if bundle.program_context.people:
        for person in bundle.program_context.people:
            rows.append(
                {
                    "display_name": person.display_name or person.email,
                    "email": person.email,
                    "workstreams": tuple(person.workstreams),
                }
            )
            displayed = True
    if displayed:
        _emit_list_output(rows, format=format, columns=("display_name", "email", "workstreams"))
        return

    for workstream in bundle.program_context.workstreams:
        if not workstream.dri_email:
            continue
        rows.append(
            {
                "display_name": workstream.dri_email,
                "email": workstream.dri_email,
                "workstreams": (workstream.name,),
            }
        )
    _emit_list_output(rows, format=format, columns=("display_name", "email", "workstreams"))


def _emit_list_output(rows: list[dict[str, object]], *, format: str, columns: tuple[str, ...]) -> None:
    if format == "json":
        typer.echo(json.dumps(rows, indent=2, sort_keys=True, default=_json_default))
        return
    if format == "csv":
        typer.echo(_render_csv(rows, columns=columns), nl=False)
        return
    if format != "human":
        raise typer.BadParameter("--format must be 'human', 'json', or 'csv'.")
    for row in rows:
        typer.echo("\t".join(_human_cell(row[column]) for column in columns))


def _render_csv(rows: list[dict[str, object]], *, columns: tuple[str, ...]) -> str:
    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_csv_cell(row[column]) for column in columns])
    return buffer.getvalue()


def _human_cell(value: object) -> str:
    if isinstance(value, tuple):
        return ", ".join(str(item) for item in value)
    return str(value)


def _csv_cell(value: object) -> str:
    if isinstance(value, tuple):
        return "|".join(str(item) for item in value)
    return str(value)


def _json_default(value: object) -> str:
    if hasattr(value, "value"):
        return str(value.value)
    raise TypeError(f"Object of type {type(value)!r} is not JSON serializable")
