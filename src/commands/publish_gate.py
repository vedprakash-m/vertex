from __future__ import annotations

import csv
from datetime import datetime, timezone
from io import StringIO
import json
from pathlib import Path

import typer

from src.commands.confirm import ConfirmResult, confirm_issue
from src.core.archive_store import find_latest_confirmed_entry, read_archive_index
from src.core.edition_resolver import get_program_output_dir, PROGRAMS_ROOT
from src.core.snapshot_store import ARCHIVE_ROOT


def _load_persona_signal_failures(
    *,
    edition: str,
    issue: int,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Load persona signal coverage artifact and extract block failures.

    Returns (failures, warnings). Failures are block-severity failures.
    Warnings are warn-severity results or stale/missing artifact notices.
    """
    root = get_program_output_dir(edition, programs_root=programs_root)
    artifact_path = root / f"issue_{issue:03d}" / f"issue_{issue:03d}.persona_signal_coverage.json"

    if not artifact_path.exists():
        return (), (f"Persona signal coverage artifact not found — skipping persona gate check",)

    try:
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return (), (f"Persona signal coverage artifact malformed — skipping persona gate check",)

    if not isinstance(payload, dict):
        return (), (f"Persona signal coverage artifact invalid — skipping persona gate check",)

    enforcement_mode = payload.get("enforcement_mode", "")
    if enforcement_mode not in ("enforce",):
        return (), ()  # shadow/warn modes do not block

    manifest_mtime = (root / f"issue_{issue:03d}" / f"issue_{issue:03d}.manifest.json").stat().st_mtime if (root / f"issue_{issue:03d}" / f"issue_{issue:03d}.manifest.json").exists() else 0
    artifact_mtime = artifact_path.stat().st_mtime
    if artifact_mtime < manifest_mtime:
        return (), (f"Persona signal coverage artifact is stale — re-run report before publishing",)

    failures: list[str] = []
    warnings: list[str] = []

    results = payload.get("results", [])
    for result in results:
        if result.get("status") != "failed":
            continue
        effective_severity = result.get("effective_severity", "")
        if effective_severity == "block":
            location = result.get("location", "unknown")
            check_id = result.get("check_id", "unknown")
            persona_id = result.get("persona_id", "unknown")
            failures.append(f"QG-P: persona check blocked — {persona_id}/{check_id} at {location}")
        elif effective_severity == "warn":
            location = result.get("location", "unknown")
            check_id = result.get("check_id", "unknown")
            persona_id = result.get("persona_id", "unknown")
            warnings.append(f"Persona warn: {persona_id}/{check_id} at {location}")

    return tuple(failures), tuple(warnings)


def publish_gate_command(
    edition: str = typer.Option(..., "--edition", help="Edition name, e.g. myprogram_weekly."),
    issue: int | None = typer.Option(None, "--issue", help="Issue number to validate. Defaults to the active issue."),
    force: bool = typer.Option(False, "--force", help="Override forceable publish-gate failures while keeping hard blocks enforced."),
    format: str = typer.Option("human", "--format", help="Output format: human, json, or csv."),
) -> None:
    archive_index = read_archive_index(edition, archive_root=ARCHIVE_ROOT)
    resolved_issue = issue if issue is not None else _next_issue_number(archive_index)

    result = confirm_issue(
        edition_name=edition,
        issue_number=resolved_issue,
        dry_run=True,
        force=force,
    )

    persona_failures, persona_warnings = _load_persona_signal_failures(
        edition=edition,
        issue=resolved_issue,
    )

    all_failures = result.failures + persona_failures
    all_warnings = result.warnings + persona_warnings

    if format != "human":
        typer.echo(render_publish_gate_output(edition, result, force=force, format=format, persona_failures=persona_failures, persona_warnings=persona_warnings), nl=False)
        raise typer.Exit(code=0 if not all_failures else max(result.exit_code, 3))

    if all_failures:
        typer.echo(f"Publish gate blocked for issue {resolved_issue:03d}.")
        for failure in all_failures:
            typer.echo(f"- {failure}")
        for warning in all_warnings:
            typer.echo(f"Warning: {warning}")
        raise typer.Exit(code=max(result.exit_code, 3))

    typer.echo(f"Publish gate passed for issue {resolved_issue:03d}.")
    for warning in all_warnings:
        typer.echo(f"Warning: {warning}")
    raise typer.Exit(code=0)


def render_publish_gate_output(
    edition: str,
    result: ConfirmResult,
    *,
    force: bool,
    format: str,
    persona_failures: tuple[str, ...] = (),
    persona_warnings: tuple[str, ...] = (),
) -> str:
    payload = _build_publish_gate_payload(
        edition, result, force=force,
        persona_failures=persona_failures,
        persona_warnings=persona_warnings,
    )
    if format == "json":
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if format == "csv":
        buffer = StringIO()
        writer = csv.writer(buffer)
        writer.writerow(("entry_type", "edition_name", "issue_number", "next_issue_number", "exit_code", "forced", "status", "message"))
        writer.writerow(
            (
                "summary",
                payload["edition_name"],
                payload["issue_number"],
                payload["next_issue_number"],
                payload["exit_code"],
                payload["forced"],
                payload["status"],
                None,
            )
        )
        for failure in payload["failures"]:  # type: ignore[attr-defined]
            writer.writerow(("failure", payload["edition_name"], payload["issue_number"], payload["next_issue_number"], payload["exit_code"], payload["forced"], payload["status"], failure))
        for warning in payload["warnings"]:  # type: ignore[attr-defined]
            writer.writerow(("warning", payload["edition_name"], payload["issue_number"], payload["next_issue_number"], payload["exit_code"], payload["forced"], payload["status"], warning))
        return buffer.getvalue()
    raise typer.BadParameter("--format must be 'human', 'json', or 'csv'.")


def _build_publish_gate_payload(
    edition: str,
    result: ConfirmResult,
    *,
    force: bool,
    persona_failures: tuple[str, ...] = (),
    persona_warnings: tuple[str, ...] = (),
) -> dict[str, object]:
    all_failures = result.failures + persona_failures
    all_warnings = result.warnings + persona_warnings
    return {
        "edition_name": edition,
        "exit_code": 0 if not all_failures else max(result.exit_code, 3),
        "failures": list(all_failures),
        "forced": force,
        "issue_number": result.issue_number,
        "next_issue_number": result.next_issue_number,
        "passed": not all_failures,
        "status": "passed" if not all_failures else "blocked",
        "warnings": list(all_warnings),
    }


def _next_issue_number(archive_index) -> int:
    latest = find_latest_confirmed_entry(archive_index)
    if latest is None:
        return 1
    return latest.issue_number + 1