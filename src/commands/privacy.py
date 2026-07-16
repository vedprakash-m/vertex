"""WS-15: `vertex privacy show` command.

Prints the privacy & data governance matrix (the canonical
`src/core/privacy_matrix.py` constants) in human-readable form. Useful
for reviewers, the privacy/DPO sign-off process, and the WS-15
acceptance check that the matrix is tracked + the runtime reflects it.
"""
from __future__ import annotations

import json

import typer

from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.privacy_matrix import (
    CHANNEL_POSTURE,
    RETENTION_DAYS,
    SIDECAR_RETENTION,
    Channel,
    channels,
    sidecar_rules,
)
from src.core.privacy_purge import run_purge


privacy_app = typer.Typer(help="Privacy & data governance matrix (WS-15).")


def _format_retention_days(days: int | None) -> str:
    if days is None:
        return "indefinite"
    if days == 0:
        return "ephemeral"
    if days >= 365:
        years = days // 365
        return f"{days}d ({years}y)"
    return f"{days}d"


@privacy_app.command("show")
def privacy_show_command(
    section: str | None = typer.Option(
        None,
        "--section",
        help="Filter to one of: channels, sidecars, retention, all (default: all).",
    ),
) -> None:
    """Print the privacy & data governance matrix.

    Sections:
      - channels: per-channel read/write classification, retention, RBAC model
      - sidecars: per-sidecar classification + retention
      - retention: retention-class → days mapping
    """
    show_channels = section in (None, "all", "channels")
    show_sidecars = section in (None, "all", "sidecars")
    show_retention = section in (None, "all", "retention")

    if show_channels:
        typer.echo("=== Channels ===")
        for channel in channels():
            p = CHANNEL_POSTURE[channel]
            write_clause = (
                f"write={p.write_default_class.value}" if p.write_default_class else "write=n/a"
            )
            typer.echo(
                f"- {channel.value}: read={p.read_default_class.value}, {write_clause}, "
                f"retention={p.retention.value} ({_format_retention_days(RETENTION_DAYS[p.retention])}), "
                f"rbac={p.rbac_model}, scopes={','.join(p.least_privilege_scopes)}"
            )
        typer.echo("")

    if show_sidecars:
        typer.echo("=== Sidecars / artifacts ===")
        for rule in sidecar_rules():
            typer.echo(
                f"- {rule.artifact_path}: class={rule.classification.value}, "
                f"retention={rule.retention.value} ({_format_retention_days(RETENTION_DAYS[rule.retention])}), "
                f"excise={'yes' if rule.supports_excise else 'no'}"
            )
        typer.echo("")

    if show_retention:
        typer.echo("=== Retention classes (days) ===")
        for cls, days in RETENTION_DAYS.items():
            typer.echo(f"- {cls.value}: {_format_retention_days(days)}")

    # Always end with the channel count so the operator can sanity-check.
    typer.echo("")
    typer.echo(f"({len(CHANNEL_POSTURE)} channels, {len(SIDECAR_RETENTION)} sidecars, "
               f"{len(RETENTION_DAYS)} retention classes)")


@privacy_app.command("check")
def privacy_check_command(
    channel: str = typer.Option(
        ...,
        "--channel",
        help="Channel name to inspect posture for (e.g. ado, kusto).",
    ),
) -> None:
    """Return the posture for a single channel (machine-friendly)."""
    try:
        ch = Channel(channel)
    except ValueError:
        typer.echo(f"Unknown channel: {channel}. Known: {', '.join(c.value for c in channels())}")
        raise typer.Exit(code=2)
    p = CHANNEL_POSTURE[ch]
    write_clause = p.write_default_class.value if p.write_default_class else "n/a"
    typer.echo(
        f"channel={p.channel.value} "
        f"read_class={p.read_default_class.value} "
        f"write_class={write_clause} "
        f"retention={p.retention.value} "
        f"days={_format_retention_days(RETENTION_DAYS[p.retention])} "
        f"rbac={p.rbac_model} "
        f"scopes={','.join(p.least_privilege_scopes)}"
    )


@privacy_app.command("purge")
def privacy_purge_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. xpf."),
    apply: bool = typer.Option(False, "--apply", help="Actually mutate sidecars. Default is dry-run (report only)."),
    format: str = typer.Option("human", "--format", help="Output format: human or json."),
) -> None:
    """WS-18/ADF-W5.9: run the unified retention purge (`src/core/privacy_purge.py`)
    for one program against every registered `SIDECAR_RETENTION` rule.
    Dry-run by default -- pass --apply to actually rewrite sidecars.
    Rules with INDEFINITE retention are skipped (never auto-purged);
    non-JSONL sidecars (SQLite, YAML config, immutable archive files) are
    recorded as no-op (governed by their own rotation/migration paths)."""
    report = run_purge(program, programs_root=PROGRAMS_ROOT, apply=apply)

    if format == "json":
        typer.echo(json.dumps(report.to_dict(), indent=2, sort_keys=True))
        return
    if format != "human":
        raise typer.BadParameter(f"Unsupported --format {format!r}; use human or json.")

    mode = "APPLIED" if apply else "DRY-RUN (pass --apply to mutate)"
    typer.echo(f"Privacy purge for {program!r} [{mode}], cutoff reference: {report.cutoff.isoformat()}")
    for record in report.records:
        if record.rows_examined == 0 and record.rows_purged == 0 and record.rows_tombstoned == 0:
            continue
        typer.echo(
            f"  {record.artifact_path}: examined={record.rows_examined} "
            f"purged={record.rows_purged} tombstoned={record.rows_tombstoned} "
            f"bytes_freed={record.bytes_freed}"
        )
    if report.skipped:
        typer.echo(f"  Skipped (indefinite retention / unresolved path): {len(report.skipped)}")
    typer.echo(
        f"Totals: rows_purged={report.total_rows_purged} "
        f"rows_tombstoned={report.total_rows_tombstoned} bytes_freed={report.total_bytes_freed}"
    )
