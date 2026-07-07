"""vertex capture-lt-deck — validate and write an lt_deck_snapshot.yaml.

γ-Read Phase 3 (§16.1): Accepts an operator-authored lt_deck_snapshot.yaml,
validates it against the schema, and writes it to programs/<prog>/knowledge/.

The QG-ED-LT doctor check warns when the snapshot is absent or > 30 days old.
"""
from __future__ import annotations

from pathlib import Path

import typer
import yaml

from src.core.edition_resolver import PROGRAMS_ROOT, resolve_edition_paths
from src.core.yaml_utils import load_yaml_mapping

_REQUIRED_TOP_KEYS = {"schema_version", "captured_at", "source", "workstreams"}
_REQUIRED_WS_KEYS = {"risk"}


def _validate_snapshot(doc: dict) -> list[str]:
    errors: list[str] = []
    missing = _REQUIRED_TOP_KEYS - set(doc.keys())
    if missing:
        errors.append(f"Missing required top-level keys: {sorted(missing)}")
    workstreams = doc.get("workstreams")
    if not isinstance(workstreams, dict) or not workstreams:
        errors.append("'workstreams' must be a non-empty mapping.")
        return errors
    for ws_name, ws_data in workstreams.items():
        if not isinstance(ws_data, dict):
            errors.append(f"Workstream {ws_name!r}: value must be a mapping.")
            continue
        missing_ws = _REQUIRED_WS_KEYS - set(ws_data.keys())
        if missing_ws:
            errors.append(f"Workstream {ws_name!r}: missing required keys {sorted(missing_ws)}.")
        risk = ws_data.get("risk", "")
        if risk not in ("high", "medium", "low", "done", "unknown"):
            errors.append(f"Workstream {ws_name!r}: invalid risk value {risk!r}.")
    return errors


def capture_lt_deck_command(
    edition: str = typer.Option(..., "--edition", help="Edition name."),
    file: Path = typer.Option(..., "--file", help="Path to the lt_deck_snapshot.yaml to validate and write."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Validate only; do not write."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, hidden=True),
) -> None:
    """Validate and write an LT deck snapshot (γ-Read Phase 3).

    The snapshot is used by QG-ED-LT to check LT deck freshness.
    Run vertex doctor --context to see the LT deck freshness status.
    """
    resolved = resolve_edition_paths(edition, programs_root=programs_root)
    if resolved is None:
        raise typer.BadParameter(f"Unknown edition {edition!r}.")
    program_id = resolved.program_id

    if not file.exists():
        raise typer.BadParameter(f"Snapshot file not found: {file}")

    doc = load_yaml_mapping(file)
    errors = _validate_snapshot(doc)
    if errors:
        typer.echo(f"Validation failed ({len(errors)} error(s)):", err=True)
        for err in errors:
            typer.echo(f"  - {err}", err=True)
        raise typer.Exit(code=1)

    if dry_run:
        ws_count = len(doc.get("workstreams") or {})
        typer.echo(f"Valid snapshot: {ws_count} workstream(s). (Dry run — not written.)")
        return

    dest = programs_root / program_id / "knowledge" / "lt_deck_snapshot.yaml"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(doc, fh, allow_unicode=True, sort_keys=True)
    ws_count = len(doc.get("workstreams") or {})
    typer.echo(f"Written {ws_count} workstream(s) to {dest}")
