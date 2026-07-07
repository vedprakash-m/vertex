from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable
from uuid import NAMESPACE_URL, uuid5

from src.commands.gather_pipeline.ado_pipeline_stage import (
    _parse_float,
    _parse_int,
)
from src.commands.gather_pipeline.ado_analytics_primitives import (
    date_from_sk as _date_from_sk,
    is_completed_state as _is_completed_state,
    parse_date_sk as _parse_date_sk,
)
from src.commands.gather_pipeline.support import (
    count_business_days_inclusive,
    format_iteration_window,
    iteration_contains_item,
    parse_iteration_date,
    summarize_iteration_capacity,
    summarize_sprint_pace,
    summarize_sprint_throughput,
)
from src.commands.gather_workiq_helpers import _truncate_signal_text
from src.core.m365_payload_support import optional_string as _optional_string
from src.core.models import Confidence, WorkItem
from src.core.models_v2 import Signal, Workstream
from src.core.signal_ref_utils import merge_entity_refs
from src.core.workstream_path_resolver import (
    resolve_workstream_id_strict_longest as _resolve_workstream_id,
)


TeamNameNormalizer = Callable[[str | None], str | None]


def build_sprint_signals(
    *,
    iterations_by_team: dict[str | None, tuple[dict[str, Any], ...]],
    capacities_by_team_iteration: dict[tuple[str | None, str | None], tuple[dict[str, Any], ...]],
    sprint_snapshot_rows: list[dict[str, Any]],
    items: tuple[WorkItem, ...],
    program_id: str,
    workstreams: tuple[Workstream, ...],
    as_of: datetime,
    normalize_ado_team_name_fn: TeamNameNormalizer,
) -> tuple[Signal, ...]:
    items_by_workstream: dict[str, list[WorkItem]] = {}
    for item in items:
        workstream_id = _resolve_workstream_id(item.area_path, workstreams)
        if workstream_id is None:
            continue
        items_by_workstream.setdefault(workstream_id, []).append(item)

    signals: list[Signal] = []
    capture_date = as_of.date().isoformat()

    for workstream in workstreams:
        team_name = normalize_ado_team_name_fn(workstream.ado_team)
        workstream_items = items_by_workstream.get(workstream.id, [])
        if not workstream_items:
            continue
        for iteration in iterations_by_team.get(team_name, ()):
            iteration_id = _optional_string(iteration.get("id"))
            iteration_path = _optional_string(iteration.get("path"))
            iteration_name = _optional_string(iteration.get("name")) or "Current sprint"
            if iteration_path is None:
                continue

            start_date = parse_iteration_date(iteration, "startDate")
            finish_date = parse_iteration_date(iteration, "finishDate")
            timeframe = _optional_string((iteration.get("attributes") or {}).get("timeFrame")) or "current"
            capacity_summary = summarize_iteration_capacity(
                capacities_by_team_iteration.get((team_name, iteration_id), ())
            )
            iteration_workstream_items = [
                item for item in workstream_items if iteration_contains_item(iteration_path, item.iteration_path)
            ]
            if not iteration_workstream_items:
                continue

            committed_item_count = len(iteration_workstream_items)
            completed_item_count = sum(1 for item in iteration_workstream_items if _is_completed_state(item.state))
            open_item_count = committed_item_count - completed_item_count
            completion_pct = round((completed_item_count / committed_item_count) * 100) if committed_item_count else 0
            open_history = _build_sprint_open_history(
                sprint_snapshot_rows,
                workstreams=workstreams,
                workstream_id=workstream.id,
                iteration_path=iteration_path,
            )
            completed_history = _build_sprint_completed_history(
                sprint_snapshot_rows,
                workstreams=workstreams,
                workstream_id=workstream.id,
                iteration_path=iteration_path,
            )
            recent_completion_summary = _summarize_recent_sprint_completion_rate(completed_history)
            previous_iteration_history = _build_previous_iteration_histories(
                sprint_snapshot_rows,
                workstreams=workstreams,
                workstream_id=workstream.id,
                current_iteration_path=iteration_path,
            )
            prev_open_raw = {} if previous_iteration_history is None else previous_iteration_history["previous_iteration_open_history"]
            prev_open: dict[str, int] = prev_open_raw if isinstance(prev_open_raw, dict) else {}
            previous_iteration_open_summary = _summarize_previous_iteration_open_count(
                previous_iteration_open_history=prev_open,
                current_open_item_count=open_item_count,
            )
            prev_completed_raw = {} if previous_iteration_history is None else previous_iteration_history["previous_iteration_completed_history"]
            prev_completed: dict[str, int] = prev_completed_raw if isinstance(prev_completed_raw, dict) else {}
            previous_iteration_throughput_summary = _summarize_previous_iteration_throughput(
                previous_iteration_completed_history=prev_completed,
                current_completion_per_business_day=None
                if recent_completion_summary is None
                else recent_completion_summary["recent_completion_per_business_day"],
            )
            three_iteration_history_summary = _summarize_three_iteration_history_metrics(
                sprint_snapshot_rows,
                workstreams=workstreams,
                workstream_id=workstream.id,
            )
            pace_summary = summarize_sprint_pace(
                start_date,
                finish_date,
                as_of=as_of,
                completion_pct=completion_pct,
            )
            throughput_summary = summarize_sprint_throughput(
                committed_item_count=committed_item_count,
                completed_item_count=completed_item_count,
                open_item_count=open_item_count,
                pace_summary=pace_summary,
                completion_pct=completion_pct,
            )
            summary_parts = [
                f"ADO sprint {iteration_name}",
                f"{committed_item_count} committed",
                f"{completed_item_count} completed",
                f"{open_item_count} open",
                f"{completion_pct}% complete",
            ]
            if previous_iteration_open_summary is not None:
                summary_parts.append(str(previous_iteration_open_summary["text"]))
            if previous_iteration_throughput_summary is not None:
                summary_parts.append(str(previous_iteration_throughput_summary["text"]))
            previous_iteration_open_history_part = _format_previous_iteration_open_history(prev_open)
            if previous_iteration_open_history_part is not None:
                summary_parts.append(previous_iteration_open_history_part)
            previous_iteration_completed_history_part = _format_previous_iteration_completed_history(prev_completed)
            if previous_iteration_completed_history_part is not None:
                summary_parts.append(previous_iteration_completed_history_part)
            if three_iteration_history_summary is not None:
                text_parts_val = three_iteration_history_summary["text_parts"]
                if isinstance(text_parts_val, tuple):
                    summary_parts.extend(text_parts_val)
            if pace_summary is not None:
                summary_parts.append(str(pace_summary["text"]))
            if throughput_summary is not None:
                summary_parts.append(str(throughput_summary["text"]))
            open_history_part = _format_open_history(open_history)
            if open_history_part is not None:
                summary_parts.append(open_history_part)
            completed_history_part = _format_completed_history(completed_history)
            if completed_history_part is not None:
                summary_parts.append(completed_history_part)
            if recent_completion_summary is not None:
                summary_parts.append(str(recent_completion_summary["text"]))
            if capacity_summary is not None:
                summary_parts.append(
                    f"team capacity {capacity_summary['total_capacity_per_day']:.1f}h/day across {capacity_summary['team_member_count']} members"
                )
            date_window = format_iteration_window(start_date, finish_date, as_of=as_of)
            if date_window is not None:
                summary_parts.append(date_window)

            raw_ref = f"ado-sprint:{workstream.id}:{team_name or 'project'}:{iteration_id or iteration_path}:{capture_date}"
            signals.append(
                Signal(
                    id=str(uuid5(NAMESPACE_URL, f"{program_id}|{raw_ref}")),
                    timestamp=as_of,
                    source="ado/sprint",
                    program_id=program_id,
                    workstream_id=workstream.id,
                    entity_refs=merge_entity_refs(
                        provider_refs=tuple(
                            f"WI:{item.id}" for item in sorted(iteration_workstream_items, key=lambda item: item.id)[:5]
                        ),
                        workstream_id=workstream.id,
                    ),
                    text=_truncate_signal_text(f"{workstream.name}: {'; '.join(summary_parts)}"),
                    raw_ref=raw_ref,
                    confidence=Confidence.HIGH,
                    metadata={
                        "workstream_id": workstream.id,
                        "ado_team": team_name,
                        "iteration_id": iteration_id,
                        "iteration_name": iteration_name,
                        "iteration_path": iteration_path,
                        "timeframe": timeframe,
                        "start_date": start_date.isoformat() if start_date is not None else None,
                        "finish_date": finish_date.isoformat() if finish_date is not None else None,
                        "committed_item_count": committed_item_count,
                        "completed_item_count": completed_item_count,
                        "open_item_count": open_item_count,
                        "open_history": open_history,
                        "completed_history": completed_history,
                        "recent_completion_per_business_day": None if recent_completion_summary is None else recent_completion_summary["recent_completion_per_business_day"],
                        "recent_completion_snapshot_count": None if recent_completion_summary is None else recent_completion_summary["recent_completion_snapshot_count"],
                        "previous_iteration_open_history": {} if previous_iteration_history is None else previous_iteration_history["previous_iteration_open_history"],
                        "previous_iteration_completed_history": {} if previous_iteration_history is None else previous_iteration_history["previous_iteration_completed_history"],
                        "previous_iteration_open_item_count": None if previous_iteration_open_summary is None else previous_iteration_open_summary["previous_iteration_open_item_count"],
                        "previous_iteration_completion_per_business_day": None if previous_iteration_throughput_summary is None else previous_iteration_throughput_summary["previous_iteration_completion_per_business_day"],
                        "three_iteration_average_completion_per_business_day": None if three_iteration_history_summary is None else three_iteration_history_summary["three_iteration_average_completion_per_business_day"],
                        "three_iteration_completion_per_business_day_history": tuple() if three_iteration_history_summary is None else three_iteration_history_summary["three_iteration_completion_per_business_day_history"],
                        "three_iteration_completed_history_series": tuple() if three_iteration_history_summary is None else three_iteration_history_summary["three_iteration_completed_history_series"],
                        "three_iteration_throughput_trend_direction": None if three_iteration_history_summary is None else three_iteration_history_summary["three_iteration_throughput_trend_direction"],
                        "three_iteration_throughput_trend_delta_per_business_day": None if three_iteration_history_summary is None else three_iteration_history_summary["three_iteration_throughput_trend_delta_per_business_day"],
                        "three_iteration_average_open_item_count": None if three_iteration_history_summary is None else three_iteration_history_summary["three_iteration_average_open_item_count"],
                        "three_iteration_open_item_count_history": tuple() if three_iteration_history_summary is None else three_iteration_history_summary["three_iteration_open_item_count_history"],
                        "three_iteration_open_history_series": tuple() if three_iteration_history_summary is None else three_iteration_history_summary["three_iteration_open_history_series"],
                        "three_iteration_open_trend_direction": None if three_iteration_history_summary is None else three_iteration_history_summary["three_iteration_open_trend_direction"],
                        "three_iteration_open_trend_delta_count": None if three_iteration_history_summary is None else three_iteration_history_summary["three_iteration_open_trend_delta_count"],
                        "historical_iteration_window_count": 0 if three_iteration_history_summary is None else three_iteration_history_summary["historical_iteration_window_count"],
                        "historical_completion_per_business_day_history": tuple() if three_iteration_history_summary is None else three_iteration_history_summary["historical_completion_per_business_day_history"],
                        "historical_completed_history_series": tuple() if three_iteration_history_summary is None else three_iteration_history_summary["historical_completed_history_series"],
                        "historical_throughput_trend_direction": None if three_iteration_history_summary is None else three_iteration_history_summary["historical_throughput_trend_direction"],
                        "historical_throughput_trend_delta_per_business_day": None if three_iteration_history_summary is None else three_iteration_history_summary["historical_throughput_trend_delta_per_business_day"],
                        "historical_open_item_count_history": tuple() if three_iteration_history_summary is None else three_iteration_history_summary["historical_open_item_count_history"],
                        "historical_open_history_series": tuple() if three_iteration_history_summary is None else three_iteration_history_summary["historical_open_history_series"],
                        "historical_open_trend_direction": None if three_iteration_history_summary is None else three_iteration_history_summary["historical_open_trend_direction"],
                        "historical_open_trend_delta_count": None if three_iteration_history_summary is None else three_iteration_history_summary["historical_open_trend_delta_count"],
                        "completion_pct": completion_pct,
                        "elapsed_business_days": None if pace_summary is None else pace_summary["elapsed_business_days"],
                        "total_business_days": None if pace_summary is None else pace_summary["total_business_days"],
                        "remaining_business_days": None if pace_summary is None else pace_summary["remaining_business_days"],
                        "expected_completion_pct": None if pace_summary is None else pace_summary["expected_completion_pct"],
                        "pace_delta_pct": None if pace_summary is None else pace_summary["pace_delta_pct"],
                        "pace_status": None if pace_summary is None else pace_summary["pace_status"],
                        "observed_completion_per_business_day": None if throughput_summary is None else throughput_summary["observed_completion_per_business_day"],
                        "required_completion_per_business_day": None if throughput_summary is None else throughput_summary["required_completion_per_business_day"],
                        "projected_completion_pct": None if throughput_summary is None else throughput_summary["projected_completion_pct"],
                        "projection_status": None if throughput_summary is None else throughput_summary["projection_status"],
                        "team_member_count": None if capacity_summary is None else capacity_summary["team_member_count"],
                        "members_with_capacity": None if capacity_summary is None else capacity_summary["members_with_capacity"],
                        "total_capacity_per_day": None if capacity_summary is None else capacity_summary["total_capacity_per_day"],
                        "days_off_entry_count": None if capacity_summary is None else capacity_summary["days_off_entry_count"],
                    },
                )
            )

    return tuple(signals)


def build_analytics_signals(
    *,
    rows: list[dict[str, Any]],
    program_id: str,
    workstreams: tuple[Workstream, ...],
    start_date_sk: int,
    end_date_sk: int,
    as_of: datetime,
) -> tuple[Signal, ...]:
    workstream_names = {workstream.id: workstream.name for workstream in workstreams}
    rows_by_workstream: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        area_path = _optional_string(row.get("AreaPath")) or ""
        workstream_id = _resolve_workstream_id(area_path, workstreams)
        if workstream_id is None:
            continue
        rows_by_workstream.setdefault(workstream_id, []).append(row)

    signals: list[Signal] = []
    for workstream_id, workstream_rows in rows_by_workstream.items():
        date_sks = [sk for row in workstream_rows if (sk := _parse_date_sk(row.get("DateSK"))) is not None]
        if not date_sks:
            continue
        earliest_date_sk = min(date_sks)
        latest_date_sk = max(date_sks)
        earliest_snapshot_date = _date_from_sk(earliest_date_sk) or as_of.date()
        latest_snapshot_date = _date_from_sk(latest_date_sk) or as_of.date()
        earliest_rows = [row for row in workstream_rows if _parse_date_sk(row.get("DateSK")) == earliest_date_sk]
        latest_rows = [row for row in workstream_rows if _parse_date_sk(row.get("DateSK")) == latest_date_sk]
        earliest_work_item_ids = {
            work_item_id
            for row in earliest_rows
            if (work_item_id := _parse_int(row.get("WorkItemId"))) is not None
        }
        latest_work_item_ids = {
            work_item_id
            for row in latest_rows
            if (work_item_id := _parse_int(row.get("WorkItemId"))) is not None
        }
        earliest_state_counts = _analytics_state_counts(earliest_rows)
        latest_state_counts = _analytics_state_counts(latest_rows)
        earliest_open_item_count = _analytics_open_item_count(earliest_rows)
        latest_open_item_count = _analytics_open_item_count(latest_rows)
        open_history = _analytics_open_history(workstream_rows)
        scope_delta_count = len(latest_work_item_ids) - len(earliest_work_item_ids)
        open_delta_count = latest_open_item_count - earliest_open_item_count
        completed_rows = _analytics_completed_rows(
            workstream_rows,
            start_date_sk=start_date_sk,
            end_date_sk=end_date_sk,
        )
        cycle_values = [
            value
            for row in completed_rows
            if (value := _parse_float(row.get("CycleTimeDays"))) is not None
        ]
        lead_values = [
            value
            for row in completed_rows
            if (value := _parse_float(row.get("LeadTimeDays"))) is not None
        ]
        average_cycle = round(sum(cycle_values) / len(cycle_values), 2) if cycle_values else None
        average_lead = round(sum(lead_values) / len(lead_values), 2) if lead_values else None
        top_states = ", ".join(
            f"{state}={count}"
            for state, count in sorted(latest_state_counts.items(), key=lambda entry: (-entry[1], entry[0]))[:3]
        )

        summary_parts = [
            f"ADO Analytics snapshot {latest_snapshot_date.isoformat()}",
            f"{len(latest_work_item_ids)} items in scope",
            f"{len(completed_rows)} completed in window",
        ]
        if average_cycle is not None:
            summary_parts.append(f"avg cycle {average_cycle:.1f}d")
        if average_lead is not None:
            summary_parts.append(f"avg lead {average_lead:.1f}d")
        if earliest_date_sk != latest_date_sk:
            summary_parts.append(_format_scope_delta(scope_delta_count, baseline_date=earliest_snapshot_date))
            summary_parts.append(_format_open_delta(open_delta_count, baseline_date=earliest_snapshot_date))
        open_history_part = _format_open_history(open_history)
        if open_history_part is not None:
            summary_parts.append(open_history_part)
        if top_states:
            summary_parts.append(f"flow: {top_states}")

        raw_ref = f"ado-analytics:{workstream_id}:{latest_date_sk}:{start_date_sk}:{end_date_sk}"
        signals.append(
            Signal(
                id=str(uuid5(NAMESPACE_URL, f"{program_id}|{raw_ref}")),
                timestamp=datetime.combine(latest_snapshot_date, time.min, tzinfo=timezone.utc),
                source="ado/analytics",
                program_id=program_id,
                workstream_id=workstream_id,
                entity_refs=merge_entity_refs(
                    provider_refs=tuple(f"WI:{work_item_id}" for work_item_id in sorted(latest_work_item_ids)[:5]),
                    workstream_id=workstream_id,
                ),
                text=_truncate_signal_text(
                    f"{workstream_names.get(workstream_id, workstream_id)}: {'; '.join(summary_parts)}"
                ),
                raw_ref=raw_ref,
                confidence=Confidence.HIGH,
                metadata={
                    "workstream_id": workstream_id,
                    "latest_snapshot_date": latest_snapshot_date.isoformat(),
                    "latest_snapshot_date_sk": latest_date_sk,
                    "window_start_snapshot_date": earliest_snapshot_date.isoformat(),
                    "window_start_snapshot_date_sk": earliest_date_sk,
                    "window_start_date_sk": start_date_sk,
                    "window_end_date_sk": end_date_sk,
                    "window_start_item_count": len(earliest_work_item_ids),
                    "snapshot_item_count": len(latest_work_item_ids),
                    "window_start_open_item_count": earliest_open_item_count,
                    "latest_open_item_count": latest_open_item_count,
                    "open_history": open_history,
                    "scope_delta_count": scope_delta_count,
                    "open_delta_count": open_delta_count,
                    "completed_item_count": len(completed_rows),
                    "average_cycle_time_days": average_cycle,
                    "average_lead_time_days": average_lead,
                    "state_counts": latest_state_counts,
                    "window_start_state_counts": earliest_state_counts,
                },
            )
        )
    return tuple(signals)
def _analytics_completed_rows(
    rows: list[dict[str, Any]],
    *,
    start_date_sk: int,
    end_date_sk: int,
) -> tuple[dict[str, Any], ...]:
    latest_completed_row_by_item: dict[int, tuple[int, dict[str, Any]]] = {}
    for row in rows:
        work_item_id = _parse_int(row.get("WorkItemId"))
        completed_date_sk = _parse_date_sk(row.get("CompletedDateSK"))
        row_date_sk = _parse_date_sk(row.get("DateSK"))
        if work_item_id is None or completed_date_sk is None or row_date_sk is None:
            continue
        if completed_date_sk < start_date_sk or completed_date_sk > end_date_sk:
            continue
        previous = latest_completed_row_by_item.get(work_item_id)
        if previous is None or row_date_sk >= previous[0]:
            latest_completed_row_by_item[work_item_id] = (row_date_sk, row)
    return tuple(row for _, row in latest_completed_row_by_item.values())


def _analytics_state_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        state = _optional_string(row.get("State"))
        if state is None:
            continue
        counts[state] = counts.get(state, 0) + 1
    return counts


def _analytics_open_item_count(rows: list[dict[str, Any]]) -> int:
    return sum(
        1
        for row in rows
        if not _is_completed_state(_optional_string(row.get("State")) or "")
    )


def _analytics_open_history(rows: list[dict[str, Any]]) -> dict[str, int]:
    open_counts_by_date_sk: dict[int, int] = {}
    for row in rows:
        date_sk = _parse_date_sk(row.get("DateSK"))
        if date_sk is None:
            continue
        if _is_completed_state(_optional_string(row.get("State")) or ""):
            open_counts_by_date_sk.setdefault(date_sk, 0)
            continue
        open_counts_by_date_sk[date_sk] = open_counts_by_date_sk.get(date_sk, 0) + 1

    open_history: dict[str, int] = {}
    for date_sk in sorted(open_counts_by_date_sk):
        snapshot_date = _date_from_sk(date_sk)
        if snapshot_date is None:
            continue
        open_history[snapshot_date.isoformat()] = open_counts_by_date_sk[date_sk]
    return open_history


def _format_open_history(open_history: dict[str, int]) -> str | None:
    if len(open_history) < 3:
        return None

    counts = list(open_history.values())[-3:]
    if len(set(counts)) == 1:
        return None
    return "burndown " + "->".join(str(count) for count in counts) + " open"


def _format_completed_history(completed_history: dict[str, int]) -> str | None:
    if len(completed_history) < 3:
        return None

    counts = list(completed_history.values())[-3:]
    if len(set(counts)) == 1:
        return None
    return "completion " + "->".join(str(count) for count in counts) + " done"


def _format_previous_iteration_open_history(previous_iteration_open_history: dict[str, int]) -> str | None:
    open_history_text = _format_open_history(previous_iteration_open_history)
    if open_history_text is None:
        return None
    return f"last sprint {open_history_text}"


def _format_previous_iteration_completed_history(previous_iteration_completed_history: dict[str, int]) -> str | None:
    completed_history_text = _format_completed_history(previous_iteration_completed_history)
    if completed_history_text is None:
        return None
    return f"last sprint {completed_history_text}"


def _build_previous_iteration_histories(
    rows: list[dict[str, Any]],
    *,
    workstreams: tuple[Workstream, ...],
    workstream_id: str,
    current_iteration_path: str,
) -> dict[str, object] | None:
    iteration_histories = _build_iteration_histories(
        rows,
        workstreams=workstreams,
        workstream_id=workstream_id,
    )
    previous_iteration_candidates = [
        (int(history["latest_date_sk"]) if isinstance(history["latest_date_sk"], (int, str)) else 0, iteration_path)
        for iteration_path, history in iteration_histories.items()
        if not iteration_contains_item(current_iteration_path, iteration_path)
    ]
    if not previous_iteration_candidates:
        return None

    _, previous_iteration_path = max(previous_iteration_candidates)
    return {
        "previous_iteration_open_history": iteration_histories[previous_iteration_path]["open_history"],
        "previous_iteration_completed_history": iteration_histories[previous_iteration_path]["completed_history"],
    }


def _build_iteration_histories(
    rows: list[dict[str, Any]],
    *,
    workstreams: tuple[Workstream, ...],
    workstream_id: str,
) -> dict[str, dict[str, object]]:
    open_counts_by_iteration_path: dict[str, dict[int, int]] = {}
    completed_counts_by_iteration_path: dict[str, dict[int, int]] = {}
    for row in rows:
        area_path = _optional_string(row.get("AreaPath")) or ""
        if _resolve_workstream_id(area_path, workstreams) != workstream_id:
            continue
        row_iteration_path = _optional_string(row.get("IterationPath"))
        if row_iteration_path is None:
            continue
        date_sk = _parse_date_sk(row.get("DateSK"))
        if date_sk is None:
            continue

        iteration_open_counts = open_counts_by_iteration_path.setdefault(row_iteration_path, {})
        iteration_completed_counts = completed_counts_by_iteration_path.setdefault(row_iteration_path, {})
        if _is_completed_state(_optional_string(row.get("State")) or ""):
            iteration_open_counts.setdefault(date_sk, 0)
            iteration_completed_counts[date_sk] = iteration_completed_counts.get(date_sk, 0) + 1
            continue
        iteration_open_counts[date_sk] = iteration_open_counts.get(date_sk, 0) + 1
        iteration_completed_counts.setdefault(date_sk, 0)

    iteration_histories: dict[str, dict[str, object]] = {}
    for iteration_path in set(open_counts_by_iteration_path) | set(completed_counts_by_iteration_path):
        date_keys = set(open_counts_by_iteration_path.get(iteration_path, {})) | set(
            completed_counts_by_iteration_path.get(iteration_path, {})
        )
        if not date_keys:
            continue
        iteration_histories[iteration_path] = {
            "latest_date_sk": max(date_keys),
            "open_history": _build_snapshot_history(open_counts_by_iteration_path.get(iteration_path, {})),
            "completed_history": _build_snapshot_history(
                completed_counts_by_iteration_path.get(iteration_path, {})
            ),
        }
    return iteration_histories


def _summarize_three_iteration_history_metrics(
    rows: list[dict[str, Any]],
    *,
    workstreams: tuple[Workstream, ...],
    workstream_id: str,
) -> dict[str, object] | None:
    iteration_histories = _build_iteration_histories(
        rows,
        workstreams=workstreams,
        workstream_id=workstream_id,
    )
    ordered_histories = sorted(
        iteration_histories.values(),
        key=lambda history: int(history["latest_date_sk"]) if isinstance(history["latest_date_sk"], (int, str)) else 0,
    )
    if len(ordered_histories) < 3:
        return None

    recent_histories = ordered_histories[-3:]
    text_parts: list[str] = []
    summary: dict[str, object] = {
        "text_parts": tuple(),
        "three_iteration_average_completion_per_business_day": None,
        "three_iteration_completion_per_business_day_history": tuple(),
        "three_iteration_completed_history_series": tuple(),
        "three_iteration_throughput_trend_direction": None,
        "three_iteration_throughput_trend_delta_per_business_day": None,
        "three_iteration_average_open_item_count": None,
        "three_iteration_open_item_count_history": tuple(),
        "three_iteration_open_history_series": tuple(),
        "three_iteration_open_trend_direction": None,
        "three_iteration_open_trend_delta_count": None,
        "historical_iteration_window_count": 0,
        "historical_completion_per_business_day_history": tuple(),
        "historical_completed_history_series": tuple(),
        "historical_throughput_trend_direction": None,
        "historical_throughput_trend_delta_per_business_day": None,
        "historical_open_item_count_history": tuple(),
        "historical_open_history_series": tuple(),
        "historical_open_trend_direction": None,
        "historical_open_trend_delta_count": None,
    }

    completion_rates: list[float] = []
    for history in recent_histories:
        completed_history_raw = history["completed_history"]
        completed_history = completed_history_raw if isinstance(completed_history_raw, dict) else {}
        completion_summary = _summarize_recent_sprint_completion_rate(completed_history)
        if completion_summary is None:
            completion_rates = []
            break
        completion_rates.append(float(completion_summary["recent_completion_per_business_day"]))
    if len(completion_rates) == 3:
        average_rate = round(sum(completion_rates) / len(completion_rates), 2)
        summary["three_iteration_average_completion_per_business_day"] = average_rate
        summary["three_iteration_completion_per_business_day_history"] = tuple(completion_rates)
        text_parts.append(f"3-sprint avg {average_rate:.1f}/day")
        text_parts.append(
            "3-sprint throughput " + "->".join(f"{rate:.1f}" for rate in completion_rates) + "/day"
        )
        throughput_trend = _summarize_numeric_trend(completion_rates)
        if throughput_trend is not None:
            summary["three_iteration_throughput_trend_direction"] = throughput_trend["direction"]
            summary["three_iteration_throughput_trend_delta_per_business_day"] = throughput_trend["delta"]
            text_parts.append(
                f"throughput trend {throughput_trend['direction']} {abs(float(throughput_trend['delta'])):.1f}/day over 3 sprints"
            )

    completed_history_series: list[tuple[int, ...]] = []
    for history in recent_histories:
        completed_history_obj = history["completed_history"]
        if not isinstance(completed_history_obj, dict):
            completed_history_series = []
            break
        counts = tuple(count for count in list(completed_history_obj.values())[-3:] if isinstance(count, int))
        if len(counts) < 3:
            completed_history_series = []
            break
        completed_history_series.append(counts)
    if len(completed_history_series) == 3:
        summary["three_iteration_completed_history_series"] = tuple(completed_history_series)
        text_parts.append(
            "3-sprint completion "
            + " | ".join("->".join(str(count) for count in counts) for counts in completed_history_series)
            + " done"
        )

    open_counts: list[int] = []
    for history in recent_histories:
        open_history_raw = history["open_history"]
        if not isinstance(open_history_raw, dict) or not open_history_raw:
            open_counts = []
            break
        last_open = list(open_history_raw.values())[-1]
        open_counts.append(last_open if isinstance(last_open, int) else 0)
    if len(open_counts) == 3:
        average_open_count = int(round(sum(open_counts) / len(open_counts)))
        summary["three_iteration_average_open_item_count"] = average_open_count
        summary["three_iteration_open_item_count_history"] = tuple(open_counts)
        text_parts.append(f"3-sprint open avg {average_open_count}")
        text_parts.append("3-sprint open " + "->".join(str(count) for count in open_counts))
        open_trend = _summarize_numeric_trend(open_counts)
        if open_trend is not None:
            summary["three_iteration_open_trend_direction"] = open_trend["direction"]
            summary["three_iteration_open_trend_delta_count"] = int(open_trend["delta"])
            text_parts.append(
                f"open trend {open_trend['direction']} {abs(int(open_trend['delta']))} over 3 sprints"
            )

    open_history_series: list[tuple[int, ...]] = []
    for history in recent_histories:
        open_history = history["open_history"]
        if not isinstance(open_history, dict):
            open_history_series = []
            break
        counts = tuple(count for count in list(open_history.values())[-3:] if isinstance(count, int))
        if len(counts) < 3:
            open_history_series = []
            break
        open_history_series.append(counts)
    if len(open_history_series) == 3:
        summary["three_iteration_open_history_series"] = tuple(open_history_series)
        text_parts.append(
            "3-sprint burndown "
            + " | ".join("->".join(str(count) for count in counts) for counts in open_history_series)
            + " open"
        )

    historical_histories = ordered_histories[-5:]
    if len(historical_histories) > 3:
        historical_window_count = len(historical_histories)
        historical_completion_rates: list[float] = []
        for history in historical_histories:
            completed_history_raw = history["completed_history"]
            completed_history = completed_history_raw if isinstance(completed_history_raw, dict) else {}
            completion_summary = _summarize_recent_sprint_completion_rate(completed_history)
            if completion_summary is None:
                historical_completion_rates = []
                break
            historical_completion_rates.append(float(completion_summary["recent_completion_per_business_day"]))
        if len(historical_completion_rates) == historical_window_count:
            summary["historical_iteration_window_count"] = historical_window_count
            summary["historical_completion_per_business_day_history"] = tuple(historical_completion_rates)
            text_parts.append(
                f"{historical_window_count}-sprint throughput "
                + "->".join(f"{rate:.1f}" for rate in historical_completion_rates)
                + "/day"
            )
            historical_throughput_trend = _summarize_numeric_trend(historical_completion_rates)
            if historical_throughput_trend is not None:
                summary["historical_throughput_trend_direction"] = historical_throughput_trend["direction"]
                summary["historical_throughput_trend_delta_per_business_day"] = historical_throughput_trend["delta"]
                text_parts.append(
                    f"historical throughput trend {historical_throughput_trend['direction']} {abs(float(historical_throughput_trend['delta'])):.1f}/day over {historical_window_count} sprints"
                )

        historical_completed_history_series: list[tuple[int, ...]] = []
        for history in historical_histories:
            completed_history_obj = history["completed_history"]
            if not isinstance(completed_history_obj, dict):
                historical_completed_history_series = []
                break
            counts = tuple(count for count in list(completed_history_obj.values())[-3:] if isinstance(count, int))
            if len(counts) < 3:
                historical_completed_history_series = []
                break
            historical_completed_history_series.append(counts)
        if len(historical_completed_history_series) == historical_window_count:
            summary["historical_iteration_window_count"] = historical_window_count
            summary["historical_completed_history_series"] = tuple(historical_completed_history_series)
            text_parts.append(
                f"{historical_window_count}-sprint completion "
                + " | ".join("->".join(str(count) for count in counts) for counts in historical_completed_history_series)
                + " done"
            )

        historical_open_counts: list[int] = []
        for history in historical_histories:
            open_history_raw = history["open_history"]
            if not isinstance(open_history_raw, dict) or not open_history_raw:
                historical_open_counts = []
                break
            last_open = list(open_history_raw.values())[-1]
            historical_open_counts.append(last_open if isinstance(last_open, int) else 0)
        if len(historical_open_counts) == historical_window_count:
            summary["historical_iteration_window_count"] = historical_window_count
            summary["historical_open_item_count_history"] = tuple(historical_open_counts)
            text_parts.append(
                f"{historical_window_count}-sprint open "
                + "->".join(str(count) for count in historical_open_counts)
            )
            historical_open_trend = _summarize_numeric_trend(historical_open_counts)
            if historical_open_trend is not None:
                summary["historical_open_trend_direction"] = historical_open_trend["direction"]
                summary["historical_open_trend_delta_count"] = int(historical_open_trend["delta"])
                text_parts.append(
                    f"historical open trend {historical_open_trend['direction']} {abs(int(historical_open_trend['delta']))} over {historical_window_count} sprints"
                )

        historical_open_history_series: list[tuple[int, ...]] = []
        for history in historical_histories:
            open_history = history["open_history"]
            if not isinstance(open_history, dict):
                historical_open_history_series = []
                break
            counts = tuple(count for count in list(open_history.values())[-3:] if isinstance(count, int))
            if len(counts) < 3:
                historical_open_history_series = []
                break
            historical_open_history_series.append(counts)
        if len(historical_open_history_series) == historical_window_count:
            summary["historical_iteration_window_count"] = historical_window_count
            summary["historical_open_history_series"] = tuple(historical_open_history_series)
            text_parts.append(
                f"{historical_window_count}-sprint burndown "
                + " | ".join("->".join(str(count) for count in counts) for counts in historical_open_history_series)
                + " open"
            )

    if not text_parts:
        return None
    summary["text_parts"] = tuple(text_parts)
    return summary


def _summarize_numeric_trend(values: list[float] | list[int]) -> dict[str, float | int | str] | None:
    if len(values) < 3:
        return None

    deltas = [current - previous for previous, current in zip(values, values[1:])]
    total_delta = values[-1] - values[0]
    if all(delta > 0 for delta in deltas):
        return {"direction": "up", "delta": total_delta}
    if all(delta < 0 for delta in deltas):
        return {"direction": "down", "delta": total_delta}
    if all(delta == 0 for delta in deltas):
        return {"direction": "flat", "delta": total_delta}
    return None


def _build_snapshot_history(counts_by_date_sk: dict[int, int]) -> dict[str, int]:
    history: dict[str, int] = {}
    for date_sk in sorted(counts_by_date_sk):
        snapshot_date = _date_from_sk(date_sk)
        if snapshot_date is None:
            continue
        history[snapshot_date.isoformat()] = counts_by_date_sk[date_sk]
    return history


def _summarize_recent_sprint_completion_rate(
    completed_history: dict[str, int],
) -> dict[str, float | int | str] | None:
    if len(completed_history) < 3:
        return None

    recent_points: list[tuple[date, int]] = []
    for raw_date, count in sorted(completed_history.items())[-3:]:
        parsed_date = _parse_date(raw_date)
        if parsed_date is None or not isinstance(count, int):
            return None
        recent_points.append((parsed_date, count))
    if len(recent_points) < 3:
        return None

    completed_delta = recent_points[-1][1] - recent_points[0][1]
    if completed_delta <= 0:
        return None

    business_days = count_business_days_inclusive(
        recent_points[0][0] + timedelta(days=1),
        recent_points[-1][0],
    )
    if business_days <= 0:
        return None

    recent_completion_per_business_day = round(completed_delta / business_days, 2)
    recent_completion_snapshot_count = len(recent_points)
    return {
        "recent_completion_per_business_day": recent_completion_per_business_day,
        "recent_completion_snapshot_count": recent_completion_snapshot_count,
        "text": f"recent {recent_completion_per_business_day:.1f}/day over {recent_completion_snapshot_count} snapshots",
    }


def _summarize_previous_iteration_open_count(
    *,
    previous_iteration_open_history: dict[str, int],
    current_open_item_count: int,
) -> dict[str, int | str] | None:
    if not previous_iteration_open_history:
        return None

    previous_iteration_open_item_count = list(previous_iteration_open_history.values())[-1]
    comparison_text = _format_sprint_open_comparison(
        current_open_item_count=current_open_item_count,
        previous_open_item_count=previous_iteration_open_item_count,
    )
    if comparison_text is None:
        return None

    return {
        "previous_iteration_open_item_count": previous_iteration_open_item_count,
        "text": comparison_text,
    }


def _summarize_previous_iteration_throughput(
    *,
    previous_iteration_completed_history: dict[str, int],
    current_completion_per_business_day: float | int | str | None,
) -> dict[str, float | str] | None:
    if not isinstance(current_completion_per_business_day, (int, float)):
        return None
    if not previous_iteration_completed_history:
        return None

    previous_rate_summary = _summarize_recent_sprint_completion_rate(previous_iteration_completed_history)
    if previous_rate_summary is None:
        return None

    previous_completion_per_business_day = previous_rate_summary["recent_completion_per_business_day"]
    if not isinstance(previous_completion_per_business_day, (int, float)):
        return None

    comparison_text = _format_sprint_throughput_comparison(
        current_completion_per_business_day=float(current_completion_per_business_day),
        previous_completion_per_business_day=float(previous_completion_per_business_day),
    )
    if comparison_text is None:
        return None

    return {
        "previous_iteration_completion_per_business_day": float(previous_completion_per_business_day),
        "text": comparison_text,
    }


def _build_sprint_open_history(
    rows: list[dict[str, Any]],
    *,
    workstreams: tuple[Workstream, ...],
    workstream_id: str,
    iteration_path: str,
) -> dict[str, int]:
    open_counts_by_date_sk: dict[int, int] = {}
    for row in rows:
        area_path = _optional_string(row.get("AreaPath")) or ""
        if _resolve_workstream_id(area_path, workstreams) != workstream_id:
            continue
        row_iteration_path = _optional_string(row.get("IterationPath"))
        if row_iteration_path is None or not iteration_contains_item(iteration_path, row_iteration_path):
            continue
        date_sk = _parse_date_sk(row.get("DateSK"))
        if date_sk is None:
            continue
        if _is_completed_state(_optional_string(row.get("State")) or ""):
            open_counts_by_date_sk.setdefault(date_sk, 0)
            continue
        open_counts_by_date_sk[date_sk] = open_counts_by_date_sk.get(date_sk, 0) + 1

    open_history: dict[str, int] = {}
    for date_sk in sorted(open_counts_by_date_sk):
        snapshot_date = _date_from_sk(date_sk)
        if snapshot_date is None:
            continue
        open_history[snapshot_date.isoformat()] = open_counts_by_date_sk[date_sk]
    return open_history


def _build_sprint_completed_history(
    rows: list[dict[str, Any]],
    *,
    workstreams: tuple[Workstream, ...],
    workstream_id: str,
    iteration_path: str,
) -> dict[str, int]:
    completed_counts_by_date_sk: dict[int, int] = {}
    for row in rows:
        area_path = _optional_string(row.get("AreaPath")) or ""
        if _resolve_workstream_id(area_path, workstreams) != workstream_id:
            continue
        row_iteration_path = _optional_string(row.get("IterationPath"))
        if row_iteration_path is None or not iteration_contains_item(iteration_path, row_iteration_path):
            continue
        date_sk = _parse_date_sk(row.get("DateSK"))
        if date_sk is None:
            continue
        if not _is_completed_state(_optional_string(row.get("State")) or ""):
            completed_counts_by_date_sk.setdefault(date_sk, 0)
            continue
        completed_counts_by_date_sk[date_sk] = completed_counts_by_date_sk.get(date_sk, 0) + 1

    completed_history: dict[str, int] = {}
    for date_sk in sorted(completed_counts_by_date_sk):
        snapshot_date = _date_from_sk(date_sk)
        if snapshot_date is None:
            continue
        completed_history[snapshot_date.isoformat()] = completed_counts_by_date_sk[date_sk]
    return completed_history


def _format_sprint_open_comparison(
    *,
    current_open_item_count: int,
    previous_open_item_count: int,
) -> str | None:
    delta = current_open_item_count - previous_open_item_count
    if delta < 0:
        return f"{abs(delta)} fewer open vs last sprint"
    if delta > 0:
        return f"{delta} more open vs last sprint"
    return None


def _format_sprint_throughput_comparison(
    *,
    current_completion_per_business_day: float,
    previous_completion_per_business_day: float,
) -> str | None:
    delta = round(current_completion_per_business_day - previous_completion_per_business_day, 2)
    if abs(delta) < 0.05:
        return "flat vs last sprint"
    direction = "faster" if delta > 0 else "slower"
    return f"{abs(delta):.1f}/day {direction} vs last sprint"


def _format_scope_delta(delta: int, *, baseline_date: date) -> str:
    if delta == 0:
        return f"scope stable vs {baseline_date.isoformat()}"
    direction = "+" if delta > 0 else ""
    return f"scope {direction}{delta} vs {baseline_date.isoformat()}"


def _format_open_delta(delta: int, *, baseline_date: date) -> str:
    if delta == 0:
        return f"open flat vs {baseline_date.isoformat()}"
    if delta < 0:
        return f"open down {abs(delta)} vs {baseline_date.isoformat()}"
    return f"open up {delta} vs {baseline_date.isoformat()}"


def _parse_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        return None

