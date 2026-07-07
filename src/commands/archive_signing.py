"""WS-7: `vertex admin archive-signing` + `vertex archive verify` commands.

Operator-off-ramp for the HMAC signing key (so a fresh dev / CI machine can
opt out of signing) and a verifier command an auditor can use to check the
sidecar of any manifest on disk.
"""
from __future__ import annotations

import os
from pathlib import Path

import typer

from src.core.archive_signing import (
    archive_signing_unavailable,
    get_archive_signing_key,
    manifest_signature_sidecar_path,
    set_archive_signing_key,
    verify_manifest_file,
)
from src.core.snapshot_store import ARCHIVE_ROOT


def admin_archive_signing_command(
    set_key: bool = typer.Option(
        False,
        "--set-key",
        help="Persist the HMAC signing key in the system keyring (reads from stdin or env VERTEX_ARCHIVE_SIGNING_KEY).",
    ),
    clear: bool = typer.Option(
        False,
        "--clear",
        help="Remove the HMAC signing key from the system keyring.",
    ),
    status: bool = typer.Option(
        False,
        "--status",
        help="Report whether a signing key is currently configured.",
    ),
    keyring_user: str = typer.Option(
        "primary",
        "--keyring-user",
        help="Keyring username to scope the key under (default: 'primary').",
    ),
) -> None:
    """Manage the HMAC key used to sign archive manifests.

    Exactly one of --set-key, --clear, or --status is required. The key is
    stored in the OS keyring under service "vertex-archive-signing"; on
    Windows that is the Windows Credential Manager. The key is never
    written to disk.
    """
    flags = [bool(set_key), bool(clear), bool(status)]
    if sum(flags) != 1:
        raise typer.BadParameter("Choose exactly one of --set-key, --clear, or --status.")

    if status:
        configured = get_archive_signing_key(username=keyring_user) is not None
        typer.echo(
            f"Archive signing key (keyring_user={keyring_user!r}): "
            f"{'configured' if configured else 'NOT configured'}"
        )
        typer.echo(
            "No signing key means confirms will land with a 'signing skipped' warning; "
            "the future QG gate (WS-7 step 2) will block confirms without a configured key."
        )
        raise typer.Exit(code=0 if configured else 1)

    if clear:
        # set_archive_signing_key overwrites; we delete by writing an empty
        # string then having the read path treat it as missing. (Windows
        # Credential Manager has no native delete via keyring>=25.)
        try:
            set_archive_signing_key("", username=keyring_user)
        except RuntimeError as exc:
            raise typer.BadParameter(str(exc))
        typer.echo(f"Archive signing key (keyring_user={keyring_user!r}) cleared.")
        raise typer.Exit(code=0)

    if set_key:
        secret = os.environ.get("VERTEX_ARCHIVE_SIGNING_KEY")
        if secret is None or not secret.strip():
            typer.echo("Enter HMAC key (input hidden on terminals that support it):", err=True)
            secret = typer.prompt("key", hide_input=True)
        if not secret or not secret.strip():
            raise typer.BadParameter("Refusing to set an empty signing key.")
        set_archive_signing_key(secret.strip(), username=keyring_user)
        typer.echo(f"Archive signing key stored (keyring_user={keyring_user!r}).")
        raise typer.Exit(code=0)


def archive_verify_command(
    edition: str = typer.Option("", "--edition", help="Edition name (e.g. myprogram_weekly)."),
    issue: int = typer.Option(0, "--issue", min=1, help="Confirmed issue number to verify (e.g. 78)."),
    archive_root: str | None = typer.Option(None, hidden=True),
    keyring_user: str = typer.Option("primary", "--keyring-user", help="Keyring username to verify with."),
) -> None:
    """Verify the HMAC signature sidecar for an archived manifest.

    Exits 0 iff the sidecar exists AND the HMAC tag matches a
    re-canonicalized hash of the on-disk manifest. Exits 2 on signature
    mismatch (the auditor should investigate). Exits 1 on missing
    sidecar (legacy / pre-signing archive, or signing skipped).
    """
    if not edition.strip():
        raise typer.BadParameter("--edition is required.")
    if issue < 1:
        raise typer.BadParameter("--issue must be a positive integer.")

    resolved_root = Path(archive_root) if archive_root is not None else ARCHIVE_ROOT
    manifest_path = resolved_root / edition / "manifests" / f"issue_{issue:03d}.json"
    sidecar_path = manifest_signature_sidecar_path(manifest_path)
    typer.echo(f"Manifest: {manifest_path}")
    typer.echo(f"Sidecar:  {sidecar_path}")

    if not manifest_path.exists():
        typer.echo(f"FAIL: manifest does not exist at {manifest_path}", err=True)
        raise typer.Exit(code=2)

    if not sidecar_path.exists():
        typer.echo("FAIL: signature sidecar missing (legacy archive or signing skipped).", err=True)
        raise typer.Exit(code=1)

    key = get_archive_signing_key(username=keyring_user)
    if key is None:
        typer.echo(
            f"FAIL: no signing key configured for keyring_user={keyring_user!r}; cannot verify.",
            err=True,
        )
        raise typer.Exit(code=2)

    if verify_manifest_file(manifest_path=manifest_path, sidecar_path=sidecar_path, key=key):
        typer.echo("OK: signature verified.")
        raise typer.Exit(code=0)
    typer.echo("FAIL: signature mismatch (manifest tampered or wrong key).", err=True)
    raise typer.Exit(code=2)
