from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from math import ceil
from pathlib import Path
from statistics import median
from typing import Iterable, Mapping

from src.core.models_v2 import TrajectoryPoint
from src.core.store_factory import build_trajectory_store_for_program_id
from src.core.view_models import KustoMetric, KustoSectionData
from src.core.work_item_states import TERMINAL_WORK_ITEM_STATES


_ACTIVE_STATES = {"active", "inprogress", "committed", "atrisk", "blocked"}
_RESOLVED_STATES = TERMINAL_WORK_ITEM_STATES - {"removed", "cut"}


@dataclass(frozen=True, slots=True)
class VelocityMetrics:
    window_days: int
    resolved_count: int
    throughput_per_week: float
    median_cycle_time_days: int | None
    p90_cycle_time_days: int | None


def build_velocity_metrics(
    trajectories_by_item: Mapping[int, tuple[TrajectoryPoint, ...]],
    *,
    as_of: date,
    window_days: int,
) -> VelocityMetrics | None:
    window_start = as_of - timedelta(days=window_days)
    cycle_times: list[int] = []
    resolved_count = 0

    for trajectory in trajectories_by_item.values():
        active_start: date | None = None
        previous_state: str | None = None
        for point in trajectory:
            normalized_state = _normalize_state(point.state)
            if normalized_state in _ACTIVE_STATES and previous_state not in _ACTIVE_STATES:
                active_start = point.date
            if normalized_state in _RESOLVED_STATES and previous_state not in _RESOLVED_STATES and active_start is not None:
                if window_start <= point.date <= as_of:
                    resolved_count += 1
                    cycle_times.append((point.date - active_start).days)
                active_start = None
            previous_state = normalized_state

    if not trajectories_by_item:
        return None

    if not cycle_times:
        return VelocityMetrics(
            window_days=window_days,
            resolved_count=resolved_count,
            throughput_per_week=resolved_count / max(window_days / 7, 1),
            median_cycle_time_days=None,
            p90_cycle_time_days=None,
        )

    return VelocityMetrics(
        window_days=window_days,
        resolved_count=resolved_count,
        throughput_per_week=resolved_count / max(window_days / 7, 1),
        median_cycle_time_days=int(round(median(cycle_times))),
        p90_cycle_time_days=_percentile(cycle_times, 0.9),
    )


def build_velocity_kusto_section(
    *,
    program_id: str,
    item_ids: Iterable[int],
    as_of: date,
    window_days: int,
    programs_root: Path,
    section_id: str = "trajectory-velocity",
    title: str = "Trajectory Velocity",
    query_id: str = "trajectory-velocity",
    source_label: str = "ADO trajectory fallback",
    confidence: str = "medium",
    caveats: tuple[str, ...] = ("Derived from trajectory state transitions because Kusto is disabled.",),
) -> KustoSectionData | None:
    trajectory_store = build_trajectory_store_for_program_id(
        program_id,
        programs_root=programs_root,
    )
    trajectories_by_item = {
        item_id: trajectory_store.read(program_id, item_id)
        for item_id in item_ids
    }
    metrics = build_velocity_metrics(trajectories_by_item, as_of=as_of, window_days=window_days)
    if metrics is None:
        return None

    return KustoSectionData(
        section_id=section_id,
        title=title,
        query_id=query_id,
        render_mode="metric_highlight",
        source_label=source_label,
        confidence=confidence,
        columns=(),
        rows=(),
        metrics=(
            KustoMetric(label="Resolved items", value=str(metrics.resolved_count)),
            KustoMetric(label="Throughput", value=f"{_format_number(metrics.throughput_per_week)}/week"),
            KustoMetric(label="Median cycle time", value=_format_duration_days(metrics.median_cycle_time_days)),
            KustoMetric(label="P90 cycle time", value=_format_duration_days(metrics.p90_cycle_time_days)),
        ),
        image_data_url=None,
        reference_url=None,
        caveats=caveats,
        message=None,
        is_degraded=False,
    )


def _normalize_state(value: str) -> str:
    return "".join(ch for ch in value.strip().lower() if ch.isalnum())


def _percentile(values: list[int], percentile: float) -> int:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = max(0, ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _format_number(value: float) -> str:
    rounded = round(value, 1)
    return str(int(rounded)) if rounded.is_integer() else f"{rounded:.1f}"


def _format_duration_days(value: int | None) -> str:
    if value is None:
        return "Unavailable"
    return f"{value}d"