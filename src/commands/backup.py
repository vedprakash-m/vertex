from __future__ import annotations

import csv
from io import StringIO
import json
from pathlib import Path

import typer

from src.core.backup import (
    BACKUP_MANIFEST_NAME,
    REPO_ROOT,
    create_repository_backup,
    restore_repository_backup,
    verify_repository_backup,
)
from src.core.exceptions import StateError


def backup_command(
    to: Path | None = typer.Option(None, "--to", help="Destination directory for a backup snapshot."),
    verify: Path | None = typer.Option(None, "--verify", help="Existing backup directory to verify."),
    restore: Path | None = typer.Option(
        None,
        "--restore",
        help="WS-23: restore a backup snapshot to a destination. Pass the destination directory; --from specifies the backup root.",
    ),
    from_backup: Path | None = typer.Option(
        None,
        "--from",
        help="WS-23: backup directory to restore from (used with --restore).",
    ),
    skip_preflight: bool = typer.Option(
        False,
        "--skip-preflight",
        help="WS-23: skip the backup verify step before restore. Off by default; only the clean-machine drill may override.",
    ),
    format: str = typer.Option("human", "--format", help="Output format: human, json, or csv."),
) -> None:
    # Exactly one of --to, --verify, --restore must be set.
    option_count = sum(1 for opt in (to, verify, restore) if opt is not None)
    if option_count != 1:
        raise typer.BadParameter("Provide exactly one of --to, --verify, or --restore.")

    if restore is not None:
        if from_backup is None:
            raise typer.BadParameter("--restore requires --from <backup_dir>.")
        try:
            result = restore_repository_backup(
                from_backup, restore, skip_preflight=skip_preflight
            )
        except StateError as error:
            if format == "human":
                typer.echo(f"Restore failed: {error}")
            else:
                typer.echo(
                    render_backup_restore_error_output(restore, str(error), format=format),
                    nl=False,
                )
            raise typer.Exit(code=2) from error
        if format == "human":
            typer.echo(
                f"Restore complete: {result.file_count} files restored to {result.destination_root}"
            )
            typer.echo(f"Manifest: {result.manifest_path}")
            typer.echo(f"Preflight verified: {result.preflight_verified}")
        else:
            typer.echo(render_backup_restore_output(result, format=format), nl=False)
        raise typer.Exit(code=0)

    if to is not None:
        backup_result = create_repository_backup(to, source_root=REPO_ROOT)
        if format == "human":
            typer.echo(f"Backup complete: {backup_result.file_count} files copied to {backup_result.destination_root}")
            typer.echo(f"Manifest: {backup_result.manifest_path}")
        else:
            typer.echo(render_backup_create_output(backup_result, format=format), nl=False)
        raise typer.Exit(code=0)

    assert verify is not None
    try:
        verify_result = verify_repository_backup(verify)
    except StateError as error:
        if format == "human":
            typer.echo(f"Backup verification failed: {error}")
        else:
            typer.echo(
                render_backup_verify_error_output(verify, str(error), format=format),
                nl=False,
            )
        raise typer.Exit(code=2) from error

    if verify_result.is_valid:
        if format == "human":
            typer.echo(f"Backup verified: {verify_result.checked_file_count} files checked")
            typer.echo(f"Manifest: {verify.resolve() / BACKUP_MANIFEST_NAME}")
        else:
            typer.echo(render_backup_verify_output(verify, verify_result, format=format), nl=False)
        raise typer.Exit(code=0)

    if format == "human":
        typer.echo(
            "Backup verification failed: "
            f"{len(verify_result.missing_paths)} missing, {len(verify_result.mismatched_paths)} checksum mismatches"
        )
        for relative_path in verify_result.missing_paths:
            typer.echo(f"  Missing: {relative_path}")
        for relative_path in verify_result.mismatched_paths:
            typer.echo(f"  Checksum mismatch: {relative_path}")
    else:
        typer.echo(render_backup_verify_output(verify, verify_result, format=format), nl=False)
    raise typer.Exit(code=2)


def render_backup_create_output(result, *, format: str) -> str:
    payload = {
        "mode": "create",
        "file_count": result.file_count,
        "destination_root": str(result.destination_root),
        "manifest_path": str(result.manifest_path),
    }
    if format == "json":
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if format == "csv":
        buffer = StringIO()
        writer = csv.writer(buffer)
        writer.writerow(("mode", "file_count", "destination_root", "manifest_path"))
        writer.writerow((payload["mode"], payload["file_count"], payload["destination_root"], payload["manifest_path"]))
        return buffer.getvalue()
    raise typer.BadParameter("--format must be 'human', 'json', or 'csv'.")


def render_backup_verify_error_output(backup_root: Path, error_message: str, *, format: str) -> str:
    payload = {
        "mode": "verify",
        "backup_root": str(backup_root.resolve()),
        "is_valid": False,
        "error": error_message,
    }
    if format == "json":
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if format == "csv":
        buffer = StringIO()
        writer = csv.writer(buffer)
        writer.writerow(("mode", "backup_root", "is_valid", "checked_file_count", "missing_paths", "mismatched_paths", "manifest_path", "error"))
        writer.writerow((payload["mode"], payload["backup_root"], payload["is_valid"], "", "", "", "", payload["error"]))
        return buffer.getvalue()
    raise typer.BadParameter("--format must be 'human', 'json', or 'csv'.")


def render_backup_verify_output(backup_root: Path, result, *, format: str) -> str:
    payload = {
        "mode": "verify",
        "backup_root": str(backup_root.resolve()),
        "is_valid": result.is_valid,
        "checked_file_count": result.checked_file_count,
        "missing_paths": list(result.missing_paths),
        "mismatched_paths": list(result.mismatched_paths),
        "manifest_path": str(backup_root.resolve() / BACKUP_MANIFEST_NAME),
    }
    if format == "json":
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if format == "csv":
        buffer = StringIO()
        writer = csv.writer(buffer)
        writer.writerow(("mode", "backup_root", "is_valid", "checked_file_count", "missing_paths", "mismatched_paths", "manifest_path", "error"))
        writer.writerow(
            (
                payload["mode"],
                payload["backup_root"],
                payload["is_valid"],
                payload["checked_file_count"],
                ";".join(payload["missing_paths"]),
                ";".join(payload["mismatched_paths"]),
                payload["manifest_path"],
                "",
            )
        )
        return buffer.getvalue()
    raise typer.BadParameter("--format must be 'human', 'json', or 'csv'.")


def render_backup_restore_output(result, *, format: str) -> str:
    payload = {
        "mode": "restore",
        "from": str(result.source_backup_root),
        "destination_root": str(result.destination_root),
        "file_count": result.file_count,
        "preflight_verified": result.preflight_verified,
        "manifest_path": str(result.manifest_path),
    }
    if format == "json":
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if format == "csv":
        buffer = StringIO()
        writer = csv.writer(buffer)
        writer.writerow(("mode", "from", "destination_root", "file_count", "preflight_verified", "manifest_path"))
        writer.writerow(
            (
                payload["mode"],
                payload["from"],
                payload["destination_root"],
                payload["file_count"],
                payload["preflight_verified"],
                payload["manifest_path"],
            )
        )
        return buffer.getvalue()
    raise typer.BadParameter("--format must be 'human', 'json', or 'csv'.")


def render_backup_restore_error_output(destination_root: Path, error_message: str, *, format: str) -> str:
    payload = {
        "mode": "restore",
        "destination_root": str(destination_root.resolve()),
        "is_valid": False,
        "error": error_message,
    }
    if format == "json":
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if format == "csv":
        buffer = StringIO()
        writer = csv.writer(buffer)
        writer.writerow(("mode", "destination_root", "is_valid", "error"))
        writer.writerow((payload["mode"], payload["destination_root"], payload["is_valid"], payload["error"]))
        return buffer.getvalue()
    raise typer.BadParameter("--format must be 'human', 'json', or 'csv'.")