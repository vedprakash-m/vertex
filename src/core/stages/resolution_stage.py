from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.core.archive_store import find_latest_confirmed_entry, load_previous_confirmed_snapshot, read_archive_index
from src.core.config_loader import REPORTS_ROOT, load_bundle
from src.core.edition_resolver import find_edition_yaml, resolve_edition, get_program_output_dir
from src.core.models import ArchiveIndex, EditionType
from src.core.narrative_store import delete_narratives_dir
from src.core.overrides_store import seed_overrides_from_prior
from src.core.overrides_store import delete_seedable_overrides
from src.core.pipeline import StageContext
from src.core.snapshot_store import ARCHIVE_ROOT
from src.core.snapshot_store import read_snapshot
from src.core.trusted_baseline_store import load_trusted_baseline_issue


_NATIVE_DATETIME = datetime


class ResolutionStage:
    def name(self) -> str:
        return "resolution"

    def execute(self, ctx: StageContext) -> StageContext:
        started_at = ctx.started_at or datetime.now(timezone.utc)
        resolved_reports_root = ctx.reports_root or REPORTS_ROOT
        resolved_archive_root = ctx.archive_root or ARCHIVE_ROOT
        repo_root = resolved_reports_root.parent
        programs_root = repo_root / "programs"
        editions_root = repo_root / "editions"
        data_as_of = ctx.data_as_of
        if data_as_of is None:
            data_as_of = _parse_datetime(ctx.as_of) if ctx.as_of is not None else started_at
        if data_as_of is None:
            data_as_of = started_at

        bundle = load_bundle(
            ctx.edition_name,
            reports_root=resolved_reports_root,
            programs_root=programs_root,
        )
        resolved_v2 = resolve_edition(
            ctx.edition_name,
            programs_root=programs_root,
        ) if find_edition_yaml(ctx.edition_name, programs_root=programs_root) is not None else None
        archive_index = read_archive_index(ctx.edition_name, archive_root=resolved_archive_root)
        latest_confirmed_entry = find_latest_confirmed_entry(archive_index)
        resolved_issue_number = ctx.issue_number if ctx.issue_number is not None else _next_issue_number(archive_index)
        previous_dry_run_state = _load_previous_dry_run_state(
            edition_name=ctx.edition_name,
            issue_number=resolved_issue_number,
            programs_root=programs_root,
        )
        trusted_baseline_issue_number = load_trusted_baseline_issue(
            ctx.edition_name,
            before_issue_number=resolved_issue_number,
            programs_root=programs_root,
        )
        # For non-weekly editions (quarterly, daily, deck) the program-level
        # trusted_baseline.yaml stores the weekly issue number, which is always
        # >= the edition-specific issue number — so load_trusted_baseline_issue
        # returns None.  Fall back to the edition's own archive.
        if trusted_baseline_issue_number is None:
            _prior = find_latest_confirmed_entry(
                archive_index, before_issue_number=resolved_issue_number
            )
            if _prior is not None:
                trusted_baseline_issue_number = _prior.issue_number
        if ctx.reseed:
            delete_narratives_dir(
                ctx.edition_name,
                resolved_issue_number,
                reports_root=resolved_reports_root,
            )
            delete_seedable_overrides(
                ctx.edition_name,
                resolved_issue_number,
                reports_root=resolved_reports_root,
            )
        previous_snapshot, previous_issue_number = _resolve_previous_snapshot(
            edition_name=ctx.edition_name,
            issue_number=resolved_issue_number,
            archive_index=archive_index,
            archive_root=resolved_archive_root,
            editions_root=editions_root,
            programs_root=programs_root,
            trusted_issue_number=trusted_baseline_issue_number,
        )
        resolved_edition_type = EditionType.from_string(ctx.edition_type_override or bundle.config.edition.type)
        overrides_seeding = (
            seed_overrides_from_prior(
                ctx.edition_name,
                target_issue_number=resolved_issue_number,
                source_issue_number=trusted_baseline_issue_number,
                reports_root=resolved_reports_root,
                archive_root=resolved_archive_root,
            )
            if trusted_baseline_issue_number is not None
            else None
        )

        return replace(
            ctx,
            started_at=started_at,
            data_as_of=data_as_of,
            reports_root=resolved_reports_root,
            archive_root=resolved_archive_root,
            repo_root=repo_root,
            editions_root=editions_root,
            programs_root=programs_root,
            bundle=bundle,
            resolved_v2=resolved_v2,
            archive_index=archive_index,
            latest_confirmed_entry=latest_confirmed_entry,
            resolved_issue_number=resolved_issue_number,
            previous_dry_run_state=previous_dry_run_state,
            previous_snapshot=previous_snapshot,
            previous_issue_number=previous_issue_number,
            trusted_baseline_issue_number=trusted_baseline_issue_number,
            resolved_edition_type=resolved_edition_type,
            overrides_seeding=overrides_seeding,
        )


def _next_issue_number(index: ArchiveIndex) -> int:
    if not index.issues:
        return 1
    return max(entry.issue_number for entry in index.issues) + 1


def _load_previous_dry_run_state(
    *,
    edition_name: str,
    issue_number: int,
    programs_root: Path | None = None,
) -> dict[str, Any] | None:
    path = get_program_output_dir(edition_name, programs_root=programs_root) / f"issue_{issue_number:03d}" / f"issue_{issue_number:03d}.draft.json"  # type: ignore[arg-type]
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else None


def _resolve_previous_snapshot(
    *,
    edition_name: str,
    issue_number: int,
    archive_index: ArchiveIndex,
    archive_root: Path,
    editions_root: Path,
    programs_root: Path,
    trusted_issue_number: int | None = None,
) -> tuple[Any, int | None]:
    if trusted_issue_number is not None:
        trusted_snapshot = _load_confirmed_snapshot_for_issue(archive_index, trusted_issue_number)
        if trusted_snapshot is not None:
            return trusted_snapshot, trusted_issue_number
    return load_previous_confirmed_snapshot(
        edition_name,
        issue_number,
        archive_root=archive_root,
    )


def _load_confirmed_snapshot_for_issue(index: ArchiveIndex, issue_number: int):
    for entry in index.issues:
        if entry.kind != "confirmed" or entry.issue_number != issue_number or entry.snapshot_path is None:
            continue
        snapshot_path = Path(entry.snapshot_path)
        if not snapshot_path.exists():
            return None
        return read_snapshot(snapshot_path)
    return None


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, _NATIVE_DATETIME):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        parsed = _NATIVE_DATETIME.fromisoformat(value)
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    raise ValueError(f"Unsupported datetime value: {value!r}")
