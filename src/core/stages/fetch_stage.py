from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import re

from src.core.archive_store import find_latest_confirmed_entry, read_archive_index
from src.core.edition_resolver import get_program_output_dir, PROGRAMS_ROOT
from src.core.exceptions import QueryError, QueryTimeoutError
from src.core.models import EditionType, Snapshot, WorkItem
from src.core.operation_trace import REF_TYPE_SOURCE, record_trace_link
from src.core.pipeline import StageContext
from src.core.snapshot_store import read_snapshot


class FetchStage:
    def name(self) -> str:
        return "fetch"

    def execute(self, ctx: StageContext) -> StageContext:
        if (
            ctx.bundle is None
            or ctx.archive_root is None
            or ctx.programs_root is None
            or ctx.resolved_issue_number is None
            or ctx.resolved_edition_type is None
            or ctx.data_as_of is None
        ):
            raise RuntimeError("ResolutionStage must execute before FetchStage.")
        if ctx.resolved_edition_type == EditionType.LOOKBACK or ctx.ado_calls is not None:
            return ctx

        if ctx.offline:
            snapshot, source_label = _load_offline_snapshot(
                edition_name=ctx.edition_name,
                issue_number=ctx.resolved_issue_number,
                programs_root=ctx.programs_root,
                archive_root=ctx.archive_root,
            )
            fetched_at = snapshot.ado_data_as_of
            _record_acquisition_trace(ctx, fetched_at, snapshot.items, source_label)
            return replace(
                ctx,
                data_as_of=fetched_at,
                items=_build_offline_work_items(snapshot),
                ado_calls=0,
                offline_source_label=source_label,
            )

        if ctx.work_item_loader is None:
            raise RuntimeError("FetchStage requires a live work item loader when offline mode is disabled.")

        try:
            items, ado_calls = ctx.work_item_loader(ctx.bundle, ctx.data_as_of)
        except QueryTimeoutError as error:
            guidance = f"ADO fetch timed out after {ctx.bundle.config.ado_fetch_timeout_seconds}s. Run vertex doctor to diagnose."
            cached_snapshot = _find_offline_snapshot(
                edition_name=ctx.edition_name,
                issue_number=ctx.resolved_issue_number,
                programs_root=ctx.programs_root,
                archive_root=ctx.archive_root,
            )
            if cached_snapshot is not None:
                cached_data_as_of = cached_snapshot[0].ado_data_as_of
                guidance += (
                    " Re-run with --offline to use cached data"
                    f" (last gathered: {cached_data_as_of.strftime('%Y-%m-%d %H:%M UTC')})."
                )
            raise QueryTimeoutError(guidance) from error

        _record_acquisition_trace(ctx, ctx.data_as_of, items, "live")
        return replace(
            ctx,
            items=tuple(items),
            ado_calls=int(ado_calls),
            offline_source_label=None,
        )


def _record_acquisition_trace(
    ctx: StageContext,
    fetched_at,
    items,
    mode_label: str,
) -> None:
    """ADF-W2.12 (Section 8.2.6): record the acquisition/source stage link
    under the run's shared ``correlation_id``. A no-op when no correlation
    identity was threaded (``correlation_id == ""``), so existing
    construction call sites and unit tests are unaffected. The source
    ``ref_id`` is content-shaped -- item count plus the authoritative
    data-as-of timestamp plus the mode (live/offline) -- so a re-run of an
    unchanged acquisition is dedup-idempotent (the ledger dedupes on
    ``(correlation_id, ref_type, ref_id)``) while a genuinely different
    fetch produces a distinct, meaningful ref."""
    correlation_id = ctx.correlation_id
    if not correlation_id or ctx.bundle is None or ctx.programs_root is None:
        return
    try:
        record_trace_link(
            program_id=ctx.bundle.program.id,
            correlation_id=correlation_id,
            workflow_id=ctx.workflow_id,
            run_id=ctx.run_id or correlation_id,
            stage="acquisition",
            ref_type=REF_TYPE_SOURCE,
            ref_id=f"ado:{mode_label}:{len(tuple(items))}@{fetched_at.isoformat()}",
            programs_root=ctx.programs_root,
        )
    except Exception:
        # A trace link is observability, never a render blocker. The ledger
        # writer already maps OSError/LockException; mirror the report.py
        # writer's defensive posture so a trace failure can never break a
        # real report run. (Broad except is intentional for observability
        # best-effort code only; this is the lone such site in this module.)
        return


def _load_offline_snapshot(
    *,
    edition_name: str,
    issue_number: int,
    programs_root: Path = PROGRAMS_ROOT,
    archive_root: Path,
) -> tuple[Snapshot, str]:
    cached_snapshot = _find_offline_snapshot(
        edition_name=edition_name,
        issue_number=issue_number,
        programs_root=programs_root,
        archive_root=archive_root,
    )
    if cached_snapshot is None:
        raise QueryError(
            "Offline mode requires a cached snapshot. Run `vertex report --dry-run` online first or confirm at least one issue."
        )
    return cached_snapshot


def _find_offline_snapshot(
    *,
    edition_name: str,
    issue_number: int,
    programs_root: Path = PROGRAMS_ROOT,
    archive_root: Path,
) -> tuple[Snapshot, str] | None:
    output_dir = get_program_output_dir(edition_name, programs_root=programs_root)
    output_candidates: list[tuple[int, Path]] = []
    if output_dir.exists():
        for path in output_dir.glob("issue_*/issue_*.snapshot.json"):
            issue_value = _snapshot_issue_number(path)
            if issue_value is None:
                continue
            output_candidates.append((issue_value, path))

    preferred_output_candidates = sorted(
        ((issue_value, path) for issue_value, path in output_candidates if issue_value <= issue_number),
        key=lambda entry: entry[0],
        reverse=True,
    )
    if not preferred_output_candidates:
        preferred_output_candidates = sorted(output_candidates, key=lambda entry: entry[0], reverse=True)

    for cached_issue_number, snapshot_path in preferred_output_candidates:
        if not snapshot_path.exists():
            continue
        return read_snapshot(snapshot_path), f"cached draft Issue {cached_issue_number:03d}"

    archive_index = read_archive_index(edition_name, archive_root=archive_root)
    latest_confirmed_entry = find_latest_confirmed_entry(archive_index)
    if latest_confirmed_entry is None:
        return None
    if not latest_confirmed_entry.snapshot_path:
        return None

    snapshot_path = Path(latest_confirmed_entry.snapshot_path)
    if not snapshot_path.exists():
        return None
    return read_snapshot(snapshot_path), f"confirmed snapshot Issue {latest_confirmed_entry.issue_number:03d}"


def _snapshot_issue_number(path: Path) -> int | None:
    match = re.fullmatch(r"issue_(\d+)\.snapshot\.json", path.name)
    if match is None:
        return None
    return int(match.group(1))


def _build_offline_work_items(snapshot: Snapshot) -> tuple[WorkItem, ...]:
    fetched_at = snapshot.ado_data_as_of
    return tuple(
        WorkItem(
            id=item.id,
            type=item.type,
            title=item.title,
            state=item.state,
            assigned_to=item.assigned_to,
            assigned_to_email=None,
            area_path=item.area_path,
            iteration_path="",
            target_date=item.target_date,
            risk_level=item.risk_level,
            tags=list(item.tags),
            custom_fields={},
            revisions=[],
            comments=[],
            fetched_at=fetched_at,
        )
        for item in snapshot.items
    )