from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime

from src.core.coverage_gap import CoverageGap, build_coverage_gaps, coverage_gap_confidence_label
from src.core.models import WorkItem
from src.core.models_v2 import Program, Signal, Workstream


@dataclass(frozen=True, slots=True)
class AreaCoverageRow:
    area_path: str
    workstream_id: str
    workstream_name: str
    active_item_count: int
    analytics_matches: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OrphanedItem:
    work_item_id: int
    title: str
    state: str
    area_path: str
    assigned_to: str | None


@dataclass(frozen=True, slots=True)
class GatherStatus:
    captured_at: datetime
    signal_count: int
    trajectory_updates: int


@dataclass(frozen=True, slots=True)
class ADOStatusReport:
    program_id: str
    organization: str
    project: str
    date_window_days: int
    total_active_items: int
    area_coverage: tuple[AreaCoverageRow, ...]
    unmapped_area_paths: tuple[str, ...]
    orphaned_items: tuple[OrphanedItem, ...]
    coverage_gaps: tuple[CoverageGap, ...]
    last_gather: GatherStatus | None
    saved_query_count: int


def build_ado_status_report(
    *,
    program: Program,
    workstreams: tuple[Workstream, ...],
    items: tuple[WorkItem, ...],
    approved_signals: tuple[Signal, ...],
    narratives: Mapping[str, str] | Iterable[str],
    as_of: datetime,
    area_scope_loader: Callable[[str], tuple[str, ...]],
    last_gather: GatherStatus | None,
) -> ADOStatusReport:
    if program.ado is None:
        raise ValueError(f"Program '{program.id}' is missing ado configuration.")

    area_coverage = tuple(
        AreaCoverageRow(
            area_path=area_path,
            workstream_id=workstream.id,
            workstream_name=workstream.name,
            active_item_count=sum(1 for item in items if _area_path_matches(item.area_path, area_path)),
            analytics_matches=tuple(area_scope_loader(area_path)),
        )
        for workstream in workstreams
        for area_path in workstream.area_paths
    )

    workstream_paths = tuple(area_path for workstream in workstreams for area_path in workstream.area_paths)
    unmapped_area_paths = tuple(
        sorted(area_path for area_path in program.ado.area_paths if area_path not in set(workstream_paths))
    )

    orphaned_items = tuple(
        OrphanedItem(
            work_item_id=item.id,
            title=item.title,
            state=item.state,
            area_path=item.area_path,
            assigned_to=item.assigned_to_email or item.assigned_to,
        )
        for item in sorted(items, key=lambda entry: entry.id)
        if not any(_area_path_matches(item.area_path, area_path) for area_path in workstream_paths)
    )
    orphaned_item_ids = {item.work_item_id for item in orphaned_items}
    mapped_items = tuple(item for item in items if item.id not in orphaned_item_ids)

    coverage_gaps = build_coverage_gaps(
        mapped_items,
        approved_signals=approved_signals,
        narratives=narratives,
        as_of=as_of,
        min_age_days=program.ado.date_window_days,
    )

    return ADOStatusReport(
        program_id=program.id,
        organization=program.ado.organization,
        project=program.ado.project,
        date_window_days=program.ado.date_window_days,
        total_active_items=len(items),
        area_coverage=area_coverage,
        unmapped_area_paths=unmapped_area_paths,
        orphaned_items=orphaned_items,
        coverage_gaps=coverage_gaps,
        last_gather=last_gather,
        saved_query_count=sum(len(workstream.ado_saved_query_ids) for workstream in workstreams),
    )


def render_ado_status_report(report: ADOStatusReport) -> str:
    lines = [
        f"Program: {report.program_id}",
        f"ADO Org: {report.organization} | Project: {report.project} | Date Window: {report.date_window_days} days",
        f"Active Items: {report.total_active_items}",
        "",
        "Area Path Coverage:",
    ]

    if report.area_coverage:
        for row in report.area_coverage:
            icon = "✓" if row.analytics_matches else "!"
            match_label = _pluralize(len(row.analytics_matches), "analytics scope match")
            lines.append(
                f"  {icon} {row.area_path} -> {row.workstream_id} ({row.active_item_count} active items, {match_label})"
            )
    else:
        lines.append("  (no workstream area paths configured)")

    for area_path in report.unmapped_area_paths:
        lines.append(f"  ✗ {area_path} -> NOT MAPPED to any workstream")

    lines.append("")
    lines.append(
        f"Orphaned Items: {_pluralize(len(report.orphaned_items), 'item')} in program scope but not matched to any workstream"
    )
    if report.orphaned_items:
        for item in report.orphaned_items:
            lines.append(
                f"  WI:{item.work_item_id} - {item.title} (area: {item.area_path}, state: {item.state})"
            )

    lines.append("")
    lines.append(
        f"Coverage Gaps: {_pluralize(len(report.coverage_gaps), 'active item')} with no approved signals or narrative mention in {report.date_window_days} days"
    )
    if report.coverage_gaps:
        for gap in report.coverage_gaps:
            lines.append(
                f"  WI:{gap.work_item_id} - {gap.title} ({gap.state}; {coverage_gap_confidence_label(gap)})"
            )

    lines.append("")
    if report.last_gather is None:
        lines.append("Last Gather: No journaled gather signals recorded yet.")
    else:
        lines.append(
            "Last Gather: "
            f"{report.last_gather.captured_at.isoformat()} "
            f"({report.last_gather.signal_count} signals, {report.last_gather.trajectory_updates} trajectory updates)"
        )
    lines.append(f"Saved Queries: {report.saved_query_count} configured")
    return "\n".join(lines)


def _area_path_matches(item_area_path: str, configured_area_path: str) -> bool:
    normalized = configured_area_path.rstrip("\\")
    return item_area_path == normalized or item_area_path.startswith(f"{normalized}\\")


def _pluralize(count: int, label: str) -> str:
    suffix = "" if count == 1 else "s"
    return f"{count} {label}{suffix}"