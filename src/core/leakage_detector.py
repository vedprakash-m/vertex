from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from pathlib import Path

from src.core.ado_semantics import item_owner_alias, latest_meaningful_ado_update
from src.core.journal import PROGRAMS_ROOT
from src.core.models import WorkItem
from src.core.models_v2 import Signal, TrajectoryPoint


_WI_REF_PATTERN = re.compile(r"\bWI:(\d+)\b", re.IGNORECASE)

TrajectoryLoader = Callable[[int], tuple[TrajectoryPoint, ...]]


@dataclass(frozen=True, slots=True)
class LeakageEvent:
    signal_id: str
    work_item_id: int
    signal_timestamp: datetime
    owner_alias: str | None


@dataclass(frozen=True, slots=True)
class LeakageReport:
    events: tuple[LeakageEvent, ...]
    signal_counts_by_item: dict[int, int]
    leakage_counts_by_item: dict[int, int]
    owner_leakage_ratios: dict[str, float]


def load_approved_workiq_signals(
    program_id: str,
    *,
    as_of: datetime,
    window_days: int = 7,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[Signal, ...]:
    from src.core.store_factory import build_signal_store_for_program_id

    signal_store = build_signal_store_for_program_id(program_id, programs_root=programs_root)
    review_states = signal_store.read_reviews(program_id)
    window_start = as_of - timedelta(days=window_days)
    return tuple(
        signal
        for signal in signal_store.read(program_id, start=window_start, end=as_of)
        if signal.source.startswith("workiq/")
        and signal.entity_refs
        and review_states.get(signal.id) is not None
        and review_states[signal.id].decision == "approved"
    )


def detect_leakage(
    items: tuple[WorkItem, ...],
    signals: tuple[Signal, ...],
    *,
    trajectory_loader: TrajectoryLoader,
) -> LeakageReport:
    item_lookup = {item.id: item for item in items}
    signal_counts_by_item: dict[int, int] = defaultdict(int)
    leakage_counts_by_item: dict[int, int] = defaultdict(int)
    owner_totals: dict[str, int] = defaultdict(int)
    owner_leaks: dict[str, int] = defaultdict(int)
    events: list[LeakageEvent] = []

    for signal in signals:
        if _entity_link_confidence(signal) != "high":
            continue
        referenced_items = tuple(
            sorted({work_item_id for work_item_id in _extract_work_item_ids(signal.entity_refs) if work_item_id in item_lookup})
        )
        if not referenced_items:
            continue
        for work_item_id in referenced_items:
            signal_counts_by_item[work_item_id] += 1
            item = item_lookup[work_item_id]
            owner_alias = _owner_alias(item)
            if owner_alias is not None:
                owner_totals[owner_alias] += 1
            if _has_post_signal_ado_update(item, signal.timestamp, trajectory_loader(work_item_id)):
                continue
            leakage_counts_by_item[work_item_id] += 1
            if owner_alias is not None:
                owner_leaks[owner_alias] += 1
            events.append(
                LeakageEvent(
                    signal_id=signal.id,
                    work_item_id=work_item_id,
                    signal_timestamp=_ensure_utc(signal.timestamp),
                    owner_alias=owner_alias,
                )
            )

    owner_leakage_ratios = {
        owner_alias: round(owner_leaks.get(owner_alias, 0) / total_signals, 2)
        for owner_alias, total_signals in owner_totals.items()
        if total_signals > 0
    }
    return LeakageReport(
        events=tuple(sorted(events, key=lambda event: (event.work_item_id, event.signal_timestamp, event.signal_id))),
        signal_counts_by_item=dict(signal_counts_by_item),
        leakage_counts_by_item=dict(leakage_counts_by_item),
        owner_leakage_ratios=owner_leakage_ratios,
    )


def _extract_work_item_ids(entity_refs: tuple[str, ...]) -> tuple[int, ...]:
    work_item_ids: set[int] = set()
    for ref in entity_refs:
        match = _WI_REF_PATTERN.search(ref)
        if match is None:
            continue
        work_item_ids.add(int(match.group(1)))
    return tuple(sorted(work_item_ids))


def _has_post_signal_ado_update(
    item: WorkItem,
    signal_timestamp: datetime,
    trajectory: tuple[TrajectoryPoint, ...],
    *,
    window_days: int = 7,
) -> bool:
    normalized_signal = _ensure_utc(signal_timestamp)
    window_end = normalized_signal + timedelta(days=window_days)
    latest_update = _latest_meaningful_ado_update(item)
    if latest_update is not None and normalized_signal < latest_update <= window_end:
        return True

    signal_date = normalized_signal.date()
    window_end_date = window_end.date()
    return any(signal_date < point.date <= window_end_date for point in trajectory)


def _latest_meaningful_ado_update(item: WorkItem) -> datetime | None:
    return latest_meaningful_ado_update(item)


def _iso_week_key(value) -> tuple[int, int]:
    iso_year, iso_week, _ = value.isocalendar()
    return iso_year, iso_week


def _owner_alias(item: WorkItem) -> str | None:
    return item_owner_alias(item)


def _entity_link_confidence(signal: Signal) -> str:
    metadata = signal.metadata if isinstance(signal.metadata, dict) else {}
    value = metadata.get("entity_link_confidence")
    if value is None:
        return "low"
    normalized = str(value).strip().lower()
    if normalized in {"high", "medium", "low"}:
        return normalized
    return "low"


def _ensure_utc(timestamp: datetime) -> datetime:
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)