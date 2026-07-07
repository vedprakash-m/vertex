from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess

import typer

from src.core.archive_store import read_archive_index
from src.core.snapshot_store import ARCHIVE_ROOT
from src.core.edition_resolver import EDITIONS_ROOT, PROGRAMS_ROOT
from src.core.trusted_baseline_store import (
    TrustedBaseline,
    TrustedBaselineHistoryEntry,
    get_trusted_baseline_path,
    load_trusted_baseline,
    record_rollback_drill_passed,
    save_trusted_baseline,
)


def admin_baseline_command(
    edition: str = typer.Option("", "--edition", help="Edition name (e.g. myprogram_weekly)."),
    correct: bool = typer.Option(False, "--correct", help="Apply a trusted-baseline correction."),
    record_rollback_drill: bool = typer.Option(False, "--record-rollback-drill", help="Append a rollback-drill pass record to trusted_baseline.yaml history."),
    issue: int | None = typer.Option(None, "--issue", min=1, help="Confirmed archive issue number to set as the trusted baseline."),
    reason: str | None = typer.Option(None, "--reason", help="Why the trusted baseline is being corrected."),
    checkpoint_name: str | None = typer.Option(None, "--checkpoint-name", help="Checkpoint name used for the rollback drill."),
    rollback_exit_code: int | None = typer.Option(None, "--rollback-exit-code", min=0, help="Exit code returned by the rollback command during the drill."),
    consistency_exit_code: int | None = typer.Option(None, "--consistency-exit-code", min=0, help="Exit code returned by doctor --consistency after the rollback drill."),
    lock_issue: int | None = typer.Option(None, "--lock", min=1, help="Hardlock an issue: its confirmed snapshot + overrides can no longer be overwritten."),
    unlock_issue: int | None = typer.Option(None, "--unlock", min=1, help="Remove the hardlock from an issue so it can be rebuilt/overwritten again."),
    archive_root: str | None = typer.Option(None, hidden=True),
    editions_root: str | None = typer.Option(None, hidden=True),
    programs_root: str | None = typer.Option(None, hidden=True),
) -> None:
    mode_count = sum([bool(correct), bool(record_rollback_drill), lock_issue is not None, unlock_issue is not None])
    if mode_count != 1:
        raise typer.BadParameter("Choose exactly one of --correct, --record-rollback-drill, --lock, or --unlock.")

    resolved_archive_root = Path(archive_root) if archive_root is not None else ARCHIVE_ROOT
    resolved_editions_root = Path(editions_root) if editions_root is not None else EDITIONS_ROOT
    resolved_programs_root = Path(programs_root) if programs_root is not None else PROGRAMS_ROOT

    now = datetime.now(timezone.utc)
    operator = _read_operator()

    if lock_issue is not None or unlock_issue is not None:
        baseline = load_trusted_baseline(
            edition,
            editions_root=resolved_editions_root,
            programs_root=resolved_programs_root,
        )
        if baseline is None:
            raise typer.BadParameter(f"No trusted baseline exists for {edition}; cannot lock/unlock an issue.")
        locked = set(baseline.locked_issues)
        if lock_issue is not None:
            locked.add(lock_issue)
            target, verb = lock_issue, "locked"
        else:
            assert unlock_issue is not None
            locked.discard(unlock_issue)
            target, verb = unlock_issue, "unlocked"
        lock_document = replace(baseline, locked_issues=tuple(sorted(locked)))
        path = save_trusted_baseline(
            edition,
            lock_document,
            editions_root=resolved_editions_root,
            programs_root=resolved_programs_root,
        )
        typer.echo(f"Issue {target:03d} {verb} for {edition}. Hardlocked issues: {sorted(lock_document.locked_issues)}")
        typer.echo(f"Path: {path}")
        raise typer.Exit(code=0)

    if record_rollback_drill:
        if checkpoint_name is None or not checkpoint_name.strip():
            raise typer.BadParameter("--checkpoint-name must be non-empty when --record-rollback-drill is set.")
        if rollback_exit_code is None:
            raise typer.BadParameter("--rollback-exit-code is required when --record-rollback-drill is set.")
        if consistency_exit_code is None:
            raise typer.BadParameter("--consistency-exit-code is required when --record-rollback-drill is set.")

        document = record_rollback_drill_passed(
            edition,
            recorded_at=now,
            recorded_by=operator,
            checkpoint_name=checkpoint_name,
            rollback_exit_code=rollback_exit_code,
            consistency_exit_code=consistency_exit_code,
            editions_root=resolved_editions_root,
            programs_root=resolved_programs_root,
        )
        if document is None:
            raise typer.BadParameter("Trusted baseline must exist before recording a rollback drill.")
        typer.echo(
            f"Rollback drill recorded for {edition}: checkpoint={checkpoint_name.strip()}, "
            f"rollback_exit_code={rollback_exit_code}, consistency_exit_code={consistency_exit_code}."
        )
        typer.echo(
            "Path: "
            f"{get_trusted_baseline_path(edition, editions_root=resolved_editions_root, programs_root=resolved_programs_root)}"
        )
        raise typer.Exit(code=0)

    if issue is None:
        raise typer.BadParameter("--issue is required when --correct is set.")
    normalized_reason = (reason or "").strip()
    if not normalized_reason:
        raise typer.BadParameter("--reason must be non-empty when --correct is set.")

    archive_index = read_archive_index(edition, archive_root=resolved_archive_root)
    confirmed_issues = {
        entry.issue_number
        for entry in archive_index.issues
        if entry.kind == "confirmed"
    }
    if issue not in confirmed_issues:
        raise typer.BadParameter(f"Issue {issue:03d} is not present in the confirmed archive index for {edition}.")

    current = load_trusted_baseline(
        edition,
        editions_root=resolved_editions_root,
        programs_root=resolved_programs_root,
    )
    if current is not None and current.trusted_issue_number == issue:
        typer.echo(f"Trusted baseline already points to issue {issue:03d}; no change written.")
        raise typer.Exit(code=0)

    action = "corrected"
    if current is not None and current.trusted_issue_number is not None and issue < current.trusted_issue_number:
        action = "rolled_back"

    document = TrustedBaseline(
        schema_version=(current.schema_version if current is not None else "1.0"),
        edition=edition,
        trusted_issue_number=issue,
        established_at=(current.established_at if current is not None else now),
        established_by=(current.established_by if current is not None else operator),
        notes=(current.notes if current is not None else None),
        history=(() if current is None else current.history) + (
            TrustedBaselineHistoryEntry(
                issue=issue,
                at=now,
                by=operator,
                action=action,
                reason=normalized_reason,
            ),
        ),
        last_untrusted=(current.last_untrusted if current is not None else None),
        bridge_graduated=(current.bridge_graduated if current is not None else False),
        graduated_at=(current.graduated_at if current is not None else None),
        graduation_issue=(current.graduation_issue if current is not None else None),
        locked_issues=(current.locked_issues if current is not None else ()),
    )
    path = save_trusted_baseline(
        edition,
        document,
        editions_root=resolved_editions_root,
        programs_root=resolved_programs_root,
    )
    typer.echo(f"Trusted baseline {action} to issue {issue:03d} for {edition}.")
    typer.echo(f"Path: {path}")
    raise typer.Exit(code=0)


def _read_operator() -> str | None:
    override = os.environ.get("VERTEX_AUTHOR")
    if override and override.strip():
        return override.strip()
    try:
        completed = subprocess.run(
            ["git", "config", "user.name"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        completed = None
    if completed is not None and completed.returncode == 0:
        candidate = completed.stdout.strip()
        if candidate:
            return candidate
    username = os.environ.get("USERNAME")
    if username and username.strip():
        return username.strip()
    return None
