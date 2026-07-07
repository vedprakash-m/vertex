"""Freshness gate and shared phase-1b scoping/date helpers."""
from __future__ import annotations

from collections.abc import Collection
from datetime import date, datetime, time, timezone

from src.core.models import FreshnessReport, WorkItem
from src.core.quality_gates.models import GateEvaluation


def filter_items_to_scope(
    items: tuple[WorkItem, ...],
    publishable_item_ids: set[int] | None,
) -> tuple[WorkItem, ...]:
    if publishable_item_ids is None:
        return items
    return tuple(item for item in items if item.id in publishable_item_ids)


def filter_freshness_report(
    freshness_report: FreshnessReport,
    publishable_item_ids: set[int] | None,
) -> FreshnessReport:
    if publishable_item_ids is None:
        return freshness_report

    scoped_items = tuple(
        item
        for item in freshness_report.items
        if item.work_item_id in publishable_item_ids
    )
    return FreshnessReport(
        issue_number=freshness_report.issue_number,
        items=scoped_items,
        blocks=sum(1 for item in scoped_items if item.severity == "block"),
        warns=sum(1 for item in scoped_items if item.severity == "warn"),
        infos=sum(1 for item in scoped_items if item.severity == "info"),
    )


def filter_item_ids_to_scope(
    item_ids: Collection[int],
    publishable_item_ids: set[int] | None,
) -> tuple[int, ...]:
    if publishable_item_ids is None:
        return tuple(int(item_id) for item_id in item_ids)
    return tuple(int(item_id) for item_id in item_ids if int(item_id) in publishable_item_ids)


def evaluate_freshness_gate(freshness_report: FreshnessReport) -> GateEvaluation:
    if freshness_report.blocks == 0:
        return GateEvaluation("QG-1", True, "Freshness gate passed.", 2, forceable=True)
    return GateEvaluation(
        "QG-1",
        False,
        f"Freshness gate failed with {freshness_report.blocks} blocking item(s).",
        2,
        forceable=True,
    )


def coerce_date(value: date | datetime) -> date:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).date()
    return value


def coerce_datetime(value: date | datetime) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    return datetime.combine(value, time.min, tzinfo=timezone.utc)
