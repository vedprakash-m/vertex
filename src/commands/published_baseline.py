from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

import typer

from src.core.archive_store import update_archive_issue_metadata
from src.core.config_loader import REPORTS_ROOT
from src.core.narrative_store import PublishedBaselineSyncResult, sync_published_baseline_to_target
from src.core.published_narrative_store import PreparedPublishedNarratives, load_published_narratives, prepare_published_narratives, write_published_narratives
from src.core.snapshot_store import ARCHIVE_ROOT


REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class PublishedBaselineApplyResult:
    target_issue_number: int
    applied_files: tuple[str, ...]
    skipped_files: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PublishedBaselineResult:
    prepared: PreparedPublishedNarratives
    bundle_dir: Path | None
    manifest_path: Path | None
    apply_result: PublishedBaselineApplyResult | None


def published_baseline_command(
    edition: str = typer.Option("", "--edition", help="Edition name."),
    issue: int = typer.Option(..., "--issue", help="Confirmed issue number that owns the published newsletter."),
    eml: Path | None = typer.Option(None, "--eml", exists=True, file_okay=True, dir_okay=False, help="Optional explicit published EML path."),
    target_issue: int | None = typer.Option(None, "--target-issue", help="Optional active issue number to update with the imported published narratives."),
    write: bool = typer.Option(False, "--write", help="Persist the imported published bundle and apply any safe target updates."),
) -> None:
    result = run_published_baseline_import(
        edition_name=edition,
        issue_number=issue,
        published_eml_path=eml,
        target_issue_number=target_issue,
        write=write,
        reports_root=REPORTS_ROOT,
        archive_root=ARCHIVE_ROOT,
    )

    typer.echo(f"Published baseline import for {edition} issue {issue:03d}")
    typer.echo(f"Published EML: {result.prepared.published_eml_path}")
    typer.echo(f"Generated HTML: {result.prepared.generated_html_path}")
    typer.echo(f"Imported files: {len(result.prepared.files)}")
    if result.prepared.warnings:
        typer.echo("Warnings:")
        for warning in result.prepared.warnings:
            typer.echo(f"- {warning}")
    if write:
        assert result.bundle_dir is not None
        assert result.manifest_path is not None
        typer.echo(f"Published bundle: {result.bundle_dir}")
        typer.echo(f"Import manifest: {result.manifest_path}")
    else:
        typer.echo("Dry run: no published bundle written.")

    if result.apply_result is not None:
        verb = "Applied" if write else "Would apply"
        typer.echo(f"{verb} {len(result.apply_result.applied_files)} file(s) to issue {target_issue:03d}.")
        for filename in result.apply_result.applied_files:
            typer.echo(f"- applied: {filename}")
        for filename in result.apply_result.skipped_files:
            typer.echo(f"- skipped: {filename}")

    raise typer.Exit(code=0)


def run_published_baseline_import(
    *,
    edition_name: str,
    issue_number: int,
    published_eml_path: Path | None,
    target_issue_number: int | None,
    write: bool,
    reports_root: Path,
    archive_root: Path,
) -> PublishedBaselineResult:
    prepared = prepare_published_narratives(
        edition_name,
        issue_number,
        published_eml_path=published_eml_path,
        archive_root=archive_root,
    )

    bundle_dir: Path | None = None
    manifest_path: Path | None = None
    if write:
        bundle_dir, manifest_path = write_published_narratives(prepared, archive_root=archive_root)
        update_archive_issue_metadata(
            edition_name,
            issue_number,
            {"published_eml_path": str(prepared.published_eml_path)},
            archive_root=archive_root,
        )

    apply_result = None
    if target_issue_number is not None:
        sync_result = sync_published_baseline_to_target(
            edition_name,
            target_issue_number=target_issue_number,
            source_issue_number=issue_number,
            reports_root=reports_root,
            archive_root=archive_root,
            write=write,
            published_narratives={file.filename: file.content for file in prepared.files},
            published_source_hashes={file.filename: file.source_hash for file in prepared.files},
        )
        if sync_result is not None:
            apply_result = _to_apply_result(target_issue_number, sync_result)

    return PublishedBaselineResult(
        prepared=prepared,
        bundle_dir=bundle_dir,
        manifest_path=manifest_path,
        apply_result=apply_result,
    )


def _to_apply_result(target_issue_number: int, sync_result: PublishedBaselineSyncResult) -> PublishedBaselineApplyResult:
    return PublishedBaselineApplyResult(
        target_issue_number=target_issue_number,
        applied_files=sync_result.applied_files,
        skipped_files=sync_result.skipped_files,
    )