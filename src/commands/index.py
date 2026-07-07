from __future__ import annotations

import typer

from src.core.semantic_index import (
    ARCHIVE_ROOT,
    get_semantic_index_path,
    load_semantic_index_state,
    optimize_archive_semantic_index,
    rebuild_archive_semantic_index,
)


app = typer.Typer(help="Manage the local semantic archive index.")


@app.command("rebuild")
def rebuild_command(
    edition: str = typer.Option(..., "--edition", help="Edition name to index, e.g. myprogram_weekly."),
) -> None:
    index_path = rebuild_archive_semantic_index(edition, archive_root=ARCHIVE_ROOT)
    state = load_semantic_index_state(edition, archive_root=ARCHIVE_ROOT)
    document_count = 0 if state is None else state.indexed_document_count
    typer.echo(f"Rebuilt semantic index for {edition}: {document_count} excerpts at {index_path}")


@app.command("optimize")
def optimize_command(
    edition: str = typer.Option(..., "--edition", help="Edition name to optimize, e.g. myprogram_weekly."),
    if_needed: bool = typer.Option(False, "--if-needed", help="Only optimize when more than 1000 new excerpts have been indexed since the last optimize."),
) -> None:
    performed = optimize_archive_semantic_index(edition, archive_root=ARCHIVE_ROOT, if_needed=if_needed)
    index_path = get_semantic_index_path(edition, archive_root=ARCHIVE_ROOT)
    state = load_semantic_index_state(edition, archive_root=ARCHIVE_ROOT)
    document_count = 0 if state is None else state.indexed_document_count
    if performed:
        typer.echo(f"Optimized semantic index for {edition}: {document_count} excerpts at {index_path}")
        return
    typer.echo(f"Skipped semantic index optimize for {edition}: {document_count} excerpts at {index_path}")