"""vertex storage — read-only SQLite journal inspection commands (S1A.3)."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

import typer

from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.sqlite_stores import get_program_sqlite_store_path

app = typer.Typer(help="Inspect and validate Vertex storage (read-only).")

_EXPECTED_INDEXES = frozenset(
    {
        "idx_signals_program_timestamp",
        "idx_signal_reviews_program_signal",
        "idx_signal_usage_markers_program_signal",
        "idx_signal_threads_program_signal",
        "idx_trajectory_points_program_item_date",
    }
)


def _open_readonly(db_path: Path) -> sqlite3.Connection:
    """Open an existing SQLite DB in read-only mode (no schema mutations)."""
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


@app.command("check")
def storage_check_command(
    program: str = typer.Option(..., "--program", help="Program id (e.g. myprogram)."),
    programs_root: Optional[Path] = typer.Option(None, hidden=True),
) -> None:
    """Validate SQLite journal integrity.

    Exit codes: 0=healthy, 1=integrity failure, 2=DB missing/unreadable,
    3=WAL not in force (advisory).
    """
    db_path = get_program_sqlite_store_path(program, programs_root=programs_root or PROGRAMS_ROOT)

    if not db_path.exists():
        typer.echo(f"Storage DB not found: {db_path}", err=True)
        raise typer.Exit(code=2)

    try:
        conn = _open_readonly(db_path)
    except sqlite3.OperationalError as exc:
        typer.echo(f"Cannot open DB: {exc}", err=True)
        raise typer.Exit(code=2)

    try:
        # WAL mode check (advisory — exit 3 if not WAL)
        (journal_mode,) = conn.execute("PRAGMA journal_mode").fetchone()
        if str(journal_mode).lower() != "wal":
            typer.echo(
                f"WAL not in force (journal_mode={journal_mode}). "
                "Run: PRAGMA journal_mode = WAL",
                err=True,
            )
            raise typer.Exit(code=3)

        # PRAGMA integrity_check
        rows = conn.execute("PRAGMA integrity_check").fetchall()
        result = str(rows[0][0]).lower() if rows else ""
        if result != "ok":
            typer.echo("integrity_check failed:", err=True)
            for row in rows:
                typer.echo(f"  {row[0]}", err=True)
            raise typer.Exit(code=1)

        # PRAGMA quick_check
        rows = conn.execute("PRAGMA quick_check").fetchall()
        result = str(rows[0][0]).lower() if rows else ""
        if result != "ok":
            typer.echo("quick_check failed:", err=True)
            for row in rows:
                typer.echo(f"  {row[0]}", err=True)
            raise typer.Exit(code=1)

        # Index presence
        index_rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
        present_indexes = {str(row[0]) for row in index_rows}
        missing = _EXPECTED_INDEXES - present_indexes
        if missing:
            typer.echo(f"Missing expected indexes: {sorted(missing)}", err=True)
            raise typer.Exit(code=1)
    finally:
        conn.close()

    typer.echo(f"Storage check OK: {db_path}")
    raise typer.Exit(code=0)


@app.command("stats")
def storage_stats_command(
    program: str = typer.Option(..., "--program", help="Program id (e.g. myprogram)."),
    programs_root: Optional[Path] = typer.Option(None, hidden=True),
) -> None:
    """Print signal count, trajectory count, and DB file size.

    Exit codes: 0=success, 2=DB missing/unreadable.
    """
    db_path = get_program_sqlite_store_path(program, programs_root=programs_root or PROGRAMS_ROOT)

    if not db_path.exists():
        typer.echo(f"Storage DB not found: {db_path}", err=True)
        raise typer.Exit(code=2)

    try:
        conn = _open_readonly(db_path)
    except sqlite3.OperationalError as exc:
        typer.echo(f"Cannot open DB: {exc}", err=True)
        raise typer.Exit(code=2)

    try:
        signal_count = conn.execute(
            "SELECT COUNT(*) FROM signals WHERE program_id = ?", (program,)
        ).fetchone()[0]
        review_count = conn.execute(
            "SELECT COUNT(*) FROM signal_reviews WHERE program_id = ?", (program,)
        ).fetchone()[0]
        item_count = conn.execute(
            "SELECT COUNT(DISTINCT work_item_id) FROM trajectory_points WHERE program_id = ?",
            (program,),
        ).fetchone()[0]
        point_count = conn.execute(
            "SELECT COUNT(*) FROM trajectory_points WHERE program_id = ?", (program,)
        ).fetchone()[0]
        ts_row = conn.execute(
            "SELECT MIN(timestamp), MAX(timestamp) FROM signals WHERE program_id = ?",
            (program,),
        ).fetchone()
        oldest = str(ts_row[0]) if ts_row and ts_row[0] else "\u2014"
        newest = str(ts_row[1]) if ts_row and ts_row[1] else "\u2014"
    finally:
        conn.close()

    db_size_kb = db_path.stat().st_size / 1024

    typer.echo(f"## Storage stats \u2014 {program}\n")
    typer.echo("| Metric | Value |")
    typer.echo("|--------|-------|")
    typer.echo(f"| DB path | `{db_path}` |")
    typer.echo(f"| DB size | {db_size_kb:.1f} KB |")
    typer.echo(f"| Signals | {signal_count} |")
    typer.echo(f"| Signal reviews | {review_count} |")
    typer.echo(f"| Tracked work items | {item_count} |")
    typer.echo(f"| Trajectory points | {point_count} |")
    typer.echo(f"| Oldest signal | {oldest} |")
    typer.echo(f"| Newest signal | {newest} |")
    raise typer.Exit(code=0)
