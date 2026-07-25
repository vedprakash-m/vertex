from __future__ import annotations

from datetime import datetime
from pathlib import Path

from src.core.store_factory import build_signal_store_for_program_id
from src.core.models_v2 import Signal
from src.core.signal_review import signal_is_approved_for_evidence


def build_program_telemetry_summary(
    program_id: str,
    *,
    programs_root: Path,
    as_of: datetime,
) -> str | None:
    signal_store = build_signal_store_for_program_id(program_id, programs_root=programs_root)
    signals = signal_store.read(program_id, end=as_of)
    review_states = signal_store.read_reviews(program_id)
    approved_signals = tuple(
        signal
        for signal in signals
        if signal_is_approved_for_evidence(signal, review_states)
    )
    return build_approved_telemetry_summary(approved_signals)


def build_approved_telemetry_summary(approved_signals: tuple[Signal, ...]) -> str | None:
    approved_signals = tuple(
        signal for signal in approved_signals if signal.source in {"ado/analytics", "ado/wiql", "ado/sprint", "ado/pipeline", "ado/pr"}
    )
    if not approved_signals:
        return None

    focus_signals = _focus_telemetry_signals(approved_signals)
    analytics_signal = _latest_signal_by_source(focus_signals, "ado/analytics")
    wiql_signal = _latest_signal_by_source(focus_signals, "ado/wiql")
    sprint_signal = _latest_signal_by_source(focus_signals, "ado/sprint")
    pipeline_signal = _latest_signal_by_source(focus_signals, "ado/pipeline")
    pull_request_signal = _latest_signal_by_source(focus_signals, "ado/pr")
    parts: list[str] = []
    if analytics_signal is not None:
        parts.append(_format_analytics_summary(analytics_signal))
    if wiql_signal is not None:
        parts.append(_format_wiql_summary(wiql_signal))
    if sprint_signal is not None:
        previous_sprint_signal = _previous_sprint_signal_for_workstream(focus_signals, sprint_signal)
        recent_sprint_signals = _recent_sprint_signals_for_workstream(focus_signals, sprint_signal, limit=3)
        parts.append(
            _format_sprint_summary(
                sprint_signal,
                previous_signal=previous_sprint_signal,
                recent_signals=recent_sprint_signals,
            ),
        )
    if pipeline_signal is not None:
        parts.append(_format_pipeline_summary(pipeline_signal))
    if pull_request_signal is not None:
        parts.append(_format_pull_request_summary(pull_request_signal))
    return "; ".join(part for part in parts if part) or None


def _with_signal_confidence(summary: str, signal: Signal) -> str:
    return f"{summary} ({signal.confidence.value.lower()} confidence)"


def _shares_workstream(a: Signal, b: Signal) -> bool:
    """BL-F2 decision (2026-07-24): "same workstream" between two signals
    means their workstream_ids sets share ANY common workstream, not that
    the full sets match exactly. One shared helper for all three
    signal-to-signal workstream comparisons in this module, rather than
    each reimplementing the same set-overlap test independently."""
    return bool(set(a.workstream_ids) & set(b.workstream_ids))


def _focus_telemetry_signals(signals: tuple[Signal, ...]) -> tuple[Signal, ...]:
    anchor_candidates = [signal for signal in signals if signal.source in {"ado/analytics", "ado/wiql", "ado/sprint"}]
    latest_signal = max(anchor_candidates or list(signals), key=lambda signal: (signal.timestamp, signal.id))
    return tuple(signal for signal in signals if _shares_workstream(signal, latest_signal))


def _latest_signal_by_source(signals: tuple[Signal, ...], source: str) -> Signal | None:
    matching = [signal for signal in signals if signal.source == source]
    if not matching:
        return None
    matching.sort(key=lambda signal: (signal.timestamp, signal.id))
    return matching[-1]


def _previous_sprint_signal_for_workstream(signals: tuple[Signal, ...], current_signal: Signal) -> Signal | None:
    matching = [
        signal
        for signal in signals
        if signal.source == "ado/sprint"
        and _shares_workstream(signal, current_signal)
        and signal.id != current_signal.id
    ]
    if not matching:
        return None
    matching.sort(key=lambda signal: (signal.timestamp, signal.id))
    return matching[-1]


def _recent_sprint_signals_for_workstream(
    signals: tuple[Signal, ...],
    current_signal: Signal,
    *,
    limit: int,
) -> tuple[Signal, ...]:
    matching = [
        signal
        for signal in signals
        if signal.source == "ado/sprint" and _shares_workstream(signal, current_signal)
    ]
    if not matching:
        return ()
    matching.sort(key=lambda signal: (signal.timestamp, signal.id))
    return tuple(matching[-limit:])


def _format_analytics_summary(signal: Signal) -> str:
    metadata = signal.metadata or {}
    snapshot_item_count = metadata.get("snapshot_item_count")
    completed_item_count = metadata.get("completed_item_count")
    scope_delta_count = metadata.get("scope_delta_count")
    open_delta_count = metadata.get("open_delta_count")
    open_history = metadata.get("open_history")
    average_cycle = metadata.get("average_cycle_time_days")
    average_lead = metadata.get("average_lead_time_days")
    state_counts = metadata.get("state_counts")

    parts = ["analytics"]
    if isinstance(snapshot_item_count, int):
        parts.append(f"{snapshot_item_count} scope")
    if isinstance(completed_item_count, int):
        parts.append(f"{completed_item_count} completed")
    scope_delta_part = _format_count_delta_summary(scope_delta_count, label="scope")
    if scope_delta_part is not None:
        parts.append(scope_delta_part)
    open_delta_part = _format_count_delta_summary(open_delta_count, label="open")
    if open_delta_part is not None:
        parts.append(open_delta_part)
    open_history_part = _format_open_history_summary(open_history)
    if open_history_part is not None:
        parts.append(open_history_part)
    timing_parts: list[str] = []
    if isinstance(average_cycle, (int, float)):
        timing_parts.append(f"cycle {float(average_cycle):.1f}d")
    if isinstance(average_lead, (int, float)):
        timing_parts.append(f"lead {float(average_lead):.1f}d")
    if timing_parts:
        parts.append(" / ".join(timing_parts))
    state_mix_part = _format_state_mix_summary(state_counts)
    if state_mix_part is not None:
        parts.append(state_mix_part)
    return ", ".join(parts)


def _format_wiql_summary(signal: Signal) -> str:
    metadata = signal.metadata or {}
    work_item_count = metadata.get("work_item_count")
    query_id = metadata.get("query_id")
    section_label = signal.text.split(":", 1)[0].strip() if ":" in signal.text else None

    parts = ["wiql"]
    if isinstance(section_label, str) and section_label:
        parts.append(section_label)
    elif isinstance(query_id, str) and query_id:
        parts.append(query_id)
    if isinstance(work_item_count, int):
        item_label = "item" if work_item_count == 1 else "items"
        parts.append(f"{work_item_count} {item_label}")
    return ", ".join(parts)


def _format_pipeline_summary(signal: Signal) -> str:
    metadata = signal.metadata or {}
    pipelines = metadata.get("pipelines")
    if not isinstance(pipelines, list):
        return "pipeline"

    entries: list[str] = []
    for pipeline in pipelines:
        if not isinstance(pipeline, dict):
            continue
        parts: list[str] = []
        pipeline_name = pipeline.get("pipeline_name")
        if isinstance(pipeline_name, str) and pipeline_name:
            parts.append(pipeline_name)
        failed_run_count = pipeline.get("failed_run_count")
        recent_run_count = pipeline.get("recent_run_count")
        if isinstance(failed_run_count, int) and isinstance(recent_run_count, int) and recent_run_count > 0:
            parts.append(f"{failed_run_count}/{recent_run_count} failed")
        elif isinstance(failed_run_count, int):
            parts.append(f"{failed_run_count} failed")
        latest_failure_run_id = pipeline.get("latest_failure_run_id")
        if isinstance(latest_failure_run_id, int):
            parts.append(f"latest fail #{latest_failure_run_id}")
        latest_run_id = pipeline.get("latest_run_id")
        latest_run_result = pipeline.get("latest_run_result")
        if isinstance(latest_run_id, int) and latest_run_id != latest_failure_run_id:
            latest_run_part = f"latest #{latest_run_id}"
            if isinstance(latest_run_result, str) and latest_run_result:
                latest_run_part += f" {latest_run_result}"
            parts.append(latest_run_part)
        if parts:
            entries.append(", ".join(parts))

    if not entries:
        return "pipeline"
    return "pipeline, " + " / ".join(entries)


def _format_pull_request_summary(signal: Signal) -> str:
    metadata = signal.metadata or {}
    repositories = metadata.get("repositories")
    if not isinstance(repositories, list):
        return "pull requests"

    entries: list[str] = []
    for repository in repositories:
        if not isinstance(repository, dict):
            continue
        parts: list[str] = []
        repository_name = repository.get("repository_name")
        if isinstance(repository_name, str) and repository_name:
            parts.append(repository_name)
        open_pr_count = repository.get("open_pr_count")
        if isinstance(open_pr_count, int):
            pr_label = "PR" if open_pr_count == 1 else "PRs"
            parts.append(f"{open_pr_count} open {pr_label}")
        p90_age_days = repository.get("p90_age_days")
        if isinstance(p90_age_days, (int, float)):
            parts.append(f"P90 age {float(p90_age_days):.1f}d")
        oldest_pr_id = repository.get("oldest_pr_id")
        oldest_pr_age_days = repository.get("oldest_pr_age_days")
        if isinstance(oldest_pr_id, int) and isinstance(oldest_pr_age_days, (int, float)):
            parts.append(f"oldest #{oldest_pr_id} {float(oldest_pr_age_days):.1f}d")
        if parts:
            entries.append(", ".join(parts))

    if not entries:
        return "pull requests"
    return "pull requests, " + " / ".join(entries)


def _format_state_mix_summary(state_counts: object) -> str | None:
    if not isinstance(state_counts, dict):
        return None

    ranked_states = [
        (state, count)
        for state, count in state_counts.items()
        if isinstance(state, str) and state and isinstance(count, int)
    ]
    if not ranked_states:
        return None

    top_states = sorted(ranked_states, key=lambda entry: (-entry[1], entry[0]))[:3]
    return "flow " + " / ".join(f"{state}={count}" for state, count in top_states)


def _format_count_delta_summary(delta_count: object, *, label: str) -> str | None:
    if not isinstance(delta_count, int):
        return None
    if delta_count < 0:
        return f"{label} down {abs(delta_count)}"
    if delta_count > 0:
        return f"{label} up {delta_count}"
    return f"{label} flat"


def _format_open_history_summary(open_history: object) -> str | None:
    if not isinstance(open_history, dict):
        return None

    ordered_counts = [
        count
        for _, count in sorted(open_history.items())
        if isinstance(count, int)
    ]
    if len(ordered_counts) < 3:
        return None

    recent_counts = ordered_counts[-3:]
    if len(set(recent_counts)) == 1:
        return None
    return "burndown " + "->".join(str(count) for count in recent_counts) + " open"


def _format_completed_history_summary(completed_history: object) -> str | None:
    if not isinstance(completed_history, dict):
        return None

    ordered_counts = [
        count
        for _, count in sorted(completed_history.items())
        if isinstance(count, int)
    ]
    if len(ordered_counts) < 3:
        return None

    recent_counts = ordered_counts[-3:]
    if len(set(recent_counts)) == 1:
        return None
    return "completion " + "->".join(str(count) for count in recent_counts) + " done"


def _format_previous_iteration_open_history_summary(previous_iteration_open_history: object) -> str | None:
    open_history_summary = _format_open_history_summary(previous_iteration_open_history)
    if open_history_summary is None:
        return None
    return f"last sprint {open_history_summary}"


def _format_previous_iteration_completed_history_summary(previous_iteration_completed_history: object) -> str | None:
    completed_history_summary = _format_completed_history_summary(previous_iteration_completed_history)
    if completed_history_summary is None:
        return None
    return f"last sprint {completed_history_summary}"


def _format_recent_completion_summary(
    recent_completion_per_business_day: object,
    recent_completion_snapshot_count: object,
) -> str | None:
    if not isinstance(recent_completion_per_business_day, (int, float)):
        return None
    if not isinstance(recent_completion_snapshot_count, int) or recent_completion_snapshot_count < 3:
        return None
    if float(recent_completion_per_business_day) <= 0:
        return None
    return (
        f"recent {float(recent_completion_per_business_day):.1f}/day "
        f"over {recent_completion_snapshot_count} snapshots"
    )


def _format_sprint_summary(
    signal: Signal,
    *,
    previous_signal: Signal | None = None,
    recent_signals: tuple[Signal, ...] = (),
) -> str:
    metadata = signal.metadata or {}
    iteration_name = metadata.get("iteration_name")
    committed_item_count = metadata.get("committed_item_count")
    completed_item_count = metadata.get("completed_item_count")
    completion_pct = metadata.get("completion_pct")
    open_item_count = metadata.get("open_item_count")
    open_history = metadata.get("open_history")
    completed_history = metadata.get("completed_history")
    recent_completion_per_business_day = metadata.get("recent_completion_per_business_day")
    recent_completion_snapshot_count = metadata.get("recent_completion_snapshot_count")
    previous_iteration_open_history = metadata.get("previous_iteration_open_history")
    previous_iteration_completed_history = metadata.get("previous_iteration_completed_history")
    elapsed_business_days = metadata.get("elapsed_business_days")
    total_business_days = metadata.get("total_business_days")
    remaining_business_days = metadata.get("remaining_business_days")
    pace_status = metadata.get("pace_status")
    pace_delta_pct = metadata.get("pace_delta_pct")
    expected_completion_pct = metadata.get("expected_completion_pct")
    projection_status = metadata.get("projection_status")
    projected_completion_pct = metadata.get("projected_completion_pct")
    observed_completion_per_business_day = metadata.get("observed_completion_per_business_day")
    required_completion_per_business_day = metadata.get("required_completion_per_business_day")

    parts = ["sprint"]
    if isinstance(iteration_name, str) and iteration_name:
        parts.append(iteration_name)
    if isinstance(committed_item_count, int):
        parts.append(f"{committed_item_count} committed")
    if isinstance(completed_item_count, int):
        parts.append(f"{completed_item_count} completed")
    if isinstance(completion_pct, int):
        parts.append(f"{completion_pct}% complete")
    if isinstance(open_item_count, int):
        parts.append(f"{open_item_count} open")
    open_history_part = _format_open_history_summary(open_history)
    if open_history_part is not None:
        parts.append(open_history_part)
    completed_history_part = _format_completed_history_summary(completed_history)
    if completed_history_part is not None:
        parts.append(completed_history_part)
    recent_completion_part = _format_recent_completion_summary(
        recent_completion_per_business_day,
        recent_completion_snapshot_count,
    )
    if recent_completion_part is not None:
        parts.append(recent_completion_part)
    timing_part = _format_sprint_timing_summary(
        elapsed_business_days,
        total_business_days,
        remaining_business_days,
    )
    if timing_part is not None:
        parts.append(timing_part)
    team_capacity_part = _format_sprint_team_capacity_summary(signal)
    if team_capacity_part is not None:
        parts.append(team_capacity_part)
    open_comparison_part = _format_sprint_open_comparison_summary(signal, previous_signal)
    if open_comparison_part is not None:
        parts.append(open_comparison_part)
    previous_iteration_open_history_part = _format_previous_iteration_open_history_summary(
        previous_iteration_open_history
    )
    if previous_iteration_open_history_part is not None:
        parts.append(previous_iteration_open_history_part)
    previous_iteration_completed_history_part = _format_previous_iteration_completed_history_summary(
        previous_iteration_completed_history
    )
    if previous_iteration_completed_history_part is not None:
        parts.append(previous_iteration_completed_history_part)
    pace_part = _format_sprint_pace_summary(pace_status, pace_delta_pct, expected_completion_pct)
    if pace_part is not None:
        parts.append(pace_part)
    projection_part = _format_sprint_projection_summary(
        projection_status,
        projected_completion_pct,
        observed_completion_per_business_day,
        required_completion_per_business_day,
    )
    if projection_part is not None:
        parts.append(projection_part)
    trend_part = _format_sprint_trend_summary(signal, previous_signal)
    if trend_part is not None:
        parts.append(trend_part)
    capacity_part = _format_sprint_capacity_utilization_summary(signal, previous_signal)
    if capacity_part is not None:
        parts.append(capacity_part)
    average_part = _format_sprint_average_summary(recent_signals)
    if average_part is None:
        average_part = _format_snapshot_backed_sprint_average_summary(signal)
    if average_part is not None:
        parts.append(average_part)
    throughput_history_part = _format_snapshot_backed_sprint_throughput_history_summary(signal)
    if throughput_history_part is not None:
        parts.append(throughput_history_part)
    throughput_trend_part = _format_sprint_throughput_trend_summary(recent_signals)
    if throughput_trend_part is None:
        throughput_trend_part = _format_snapshot_backed_sprint_throughput_trend_summary(signal)
    if throughput_trend_part is not None:
        parts.append(throughput_trend_part)
    historical_throughput_history_part = _format_snapshot_backed_historical_sprint_throughput_history_summary(signal)
    if historical_throughput_history_part is not None:
        parts.append(historical_throughput_history_part)
    historical_throughput_trend_part = _format_snapshot_backed_historical_sprint_throughput_trend_summary(signal)
    if historical_throughput_trend_part is not None:
        parts.append(historical_throughput_trend_part)
    capacity_average_part = _format_sprint_capacity_utilization_average_summary(recent_signals)
    if capacity_average_part is not None:
        parts.append(capacity_average_part)
    open_average_part = _format_sprint_open_average_summary(recent_signals)
    if open_average_part is None:
        open_average_part = _format_snapshot_backed_sprint_open_average_summary(signal)
    if open_average_part is not None:
        parts.append(open_average_part)
    open_history_part = _format_snapshot_backed_sprint_open_history_summary(signal)
    if open_history_part is not None:
        parts.append(open_history_part)
    historical_open_history_part = _format_snapshot_backed_historical_sprint_open_history_summary(signal)
    if historical_open_history_part is not None:
        parts.append(historical_open_history_part)
    burndown_history_series_part = _format_snapshot_backed_sprint_burndown_history_series_summary(signal)
    if burndown_history_series_part is not None:
        parts.append(burndown_history_series_part)
    completed_history_series_part = _format_snapshot_backed_sprint_completed_history_series_summary(signal)
    if completed_history_series_part is not None:
        parts.append(completed_history_series_part)
    historical_burndown_history_series_part = _format_snapshot_backed_historical_sprint_burndown_history_series_summary(signal)
    if historical_burndown_history_series_part is not None:
        parts.append(historical_burndown_history_series_part)
    historical_completed_history_series_part = _format_snapshot_backed_historical_sprint_completed_history_series_summary(signal)
    if historical_completed_history_series_part is not None:
        parts.append(historical_completed_history_series_part)
    open_trend_part = _format_sprint_open_trend_summary(recent_signals)
    if open_trend_part is None:
        open_trend_part = _format_snapshot_backed_sprint_open_trend_summary(signal)
    if open_trend_part is not None:
        parts.append(open_trend_part)
    historical_open_trend_part = _format_snapshot_backed_historical_sprint_open_trend_summary(signal)
    if historical_open_trend_part is not None:
        parts.append(historical_open_trend_part)
    return ", ".join(parts)


def _format_sprint_pace_summary(
    pace_status: object,
    pace_delta_pct: object,
    expected_completion_pct: object,
) -> str | None:
    if pace_status == "on_track":
        if isinstance(expected_completion_pct, int):
            return f"pace on track vs {expected_completion_pct}% elapsed"
        return "pace on track"
    if isinstance(pace_delta_pct, int):
        if pace_status == "ahead" and pace_delta_pct > 0:
            if isinstance(expected_completion_pct, int):
                return f"pace {pace_delta_pct}pts ahead of {expected_completion_pct}% elapsed"
            return f"pace {pace_delta_pct}pts ahead"
        if pace_status == "behind" and pace_delta_pct < 0:
            if isinstance(expected_completion_pct, int):
                return f"pace {abs(pace_delta_pct)}pts behind {expected_completion_pct}% elapsed"
            return f"pace {abs(pace_delta_pct)}pts behind"
    return None


def _format_sprint_timing_summary(
    elapsed_business_days: object,
    total_business_days: object,
    remaining_business_days: object,
) -> str | None:
    if not isinstance(elapsed_business_days, int) or not isinstance(total_business_days, int):
        return None
    if total_business_days <= 0 or elapsed_business_days < 0:
        return None

    timing = f"{elapsed_business_days}/{total_business_days} bd elapsed"
    if isinstance(remaining_business_days, int) and remaining_business_days >= 0:
        return f"{timing}, {remaining_business_days} bd left"
    return timing


def _format_sprint_projection_summary(
    projection_status: object,
    projected_completion_pct: object,
    observed_completion_per_business_day: object,
    required_completion_per_business_day: object,
) -> str | None:
    if projection_status == "complete":
        return "finished"
    rate_context: str | None = None
    if isinstance(observed_completion_per_business_day, (int, float)) and isinstance(
        required_completion_per_business_day, (int, float)
    ):
        rate_context = (
            f" at {float(observed_completion_per_business_day):.1f}/day "
            f"({float(required_completion_per_business_day):.1f}/day needed)"
        )

    if projection_status == "finish":
        if rate_context is not None:
            return f"track to finish{rate_context}"
        return "track to finish"
    if projection_status == "at_risk" and isinstance(projected_completion_pct, int):
        if rate_context is not None:
            return f"~{projected_completion_pct}% by close{rate_context}"
        return f"~{projected_completion_pct}% by close"
    return None


def _format_sprint_trend_summary(signal: Signal, previous_signal: Signal | None) -> str | None:
    current_metadata = signal.metadata or {}
    current_rate: object = current_metadata.get("observed_completion_per_business_day")
    previous_rate: object = None
    if previous_signal is not None:
        previous_rate = (previous_signal.metadata or {}).get("observed_completion_per_business_day")
    if not isinstance(previous_rate, (int, float)):
        current_rate = current_metadata.get("recent_completion_per_business_day")
        previous_rate = current_metadata.get("previous_iteration_completion_per_business_day")
    if not isinstance(current_rate, (int, float)) or not isinstance(previous_rate, (int, float)):
        return None

    delta = round(float(current_rate) - float(previous_rate), 2)
    if abs(delta) < 0.05:
        return "flat vs last sprint"
    direction = "faster" if delta > 0 else "slower"
    return f"{abs(delta):.1f}/day {direction} vs last sprint"


def _format_sprint_open_comparison_summary(signal: Signal, previous_signal: Signal | None) -> str | None:
    current_open_count = (signal.metadata or {}).get("open_item_count")
    previous_open_count: object = None
    if previous_signal is not None:
        previous_open_count = (previous_signal.metadata or {}).get("open_item_count")
    if not isinstance(previous_open_count, int):
        previous_open_count = (signal.metadata or {}).get("previous_iteration_open_item_count")
    if not isinstance(current_open_count, int) or not isinstance(previous_open_count, int):
        return None

    delta = current_open_count - previous_open_count
    if delta < 0:
        return f"{abs(delta)} fewer open vs last sprint"
    if delta > 0:
        return f"{delta} more open vs last sprint"
    return None


def _format_sprint_team_capacity_summary(signal: Signal) -> str | None:
    metadata = signal.metadata or {}
    team_member_count = metadata.get("team_member_count")
    members_with_capacity = metadata.get("members_with_capacity")
    total_capacity_per_day = metadata.get("total_capacity_per_day")
    days_off_entry_count = metadata.get("days_off_entry_count")
    if not isinstance(team_member_count, int) or team_member_count <= 0:
        return None
    if not isinstance(total_capacity_per_day, (int, float)):
        return None

    summary = f"team cap {float(total_capacity_per_day):.1f}h/day across {team_member_count} members"
    if (
        isinstance(members_with_capacity, int)
        and 0 <= members_with_capacity < team_member_count
    ):
        summary += f", {members_with_capacity} with cap"
    if isinstance(days_off_entry_count, int) and days_off_entry_count > 0:
        day_label = "day off" if days_off_entry_count == 1 else "days off"
        summary += f", {days_off_entry_count} {day_label}"
    return summary


def _format_sprint_capacity_utilization_summary(
    signal: Signal,
    previous_signal: Signal | None,
) -> str | None:
    if previous_signal is None:
        return None

    current_utilization = _calculate_capacity_utilization_pct(signal)
    previous_utilization = _calculate_capacity_utilization_pct(previous_signal)
    if current_utilization is None or previous_utilization is None:
        return None

    return f"capacity util {current_utilization}% vs {previous_utilization}% last sprint"


def _calculate_capacity_utilization_pct(signal: Signal) -> int | None:
    metadata = signal.metadata or {}
    observed_rate = metadata.get("observed_completion_per_business_day")
    required_rate = metadata.get("required_completion_per_business_day")
    if not isinstance(observed_rate, (int, float)) or not isinstance(required_rate, (int, float)):
        return None
    if float(required_rate) <= 0:
        return None

    utilization_pct = round((float(observed_rate) / float(required_rate)) * 100)
    return int(utilization_pct)


def _format_sprint_average_summary(recent_signals: tuple[Signal, ...]) -> str | None:
    rates: list[float] = []
    for signal in recent_signals:
        rate = (signal.metadata or {}).get("observed_completion_per_business_day")
        if isinstance(rate, (int, float)):
            rates.append(float(rate))
    if len(rates) < 3:
        return None

    average_rate = round(sum(rates) / len(rates), 2)
    return f"3-sprint avg {average_rate:.1f}/day"


def _format_snapshot_backed_sprint_average_summary(signal: Signal) -> str | None:
    average_rate = (signal.metadata or {}).get("three_iteration_average_completion_per_business_day")
    if not isinstance(average_rate, (int, float)):
        return None
    return f"3-sprint avg {float(average_rate):.1f}/day"

def _format_snapshot_backed_sprint_throughput_history_summary(signal: Signal) -> str | None:
    history = (signal.metadata or {}).get("three_iteration_completion_per_business_day_history")
    if not isinstance(history, (list, tuple)) or len(history) < 3:
        return None
    rates = [float(rate) for rate in history if isinstance(rate, (int, float))]
    if len(rates) < 3:
        return None
    return "3-sprint throughput " + "->".join(f"{rate:.1f}" for rate in rates[-3:]) + "/day"


def _format_snapshot_backed_sprint_completed_history_series_summary(signal: Signal) -> str | None:
    history_series = (signal.metadata or {}).get("three_iteration_completed_history_series")
    if not isinstance(history_series, (list, tuple)) or len(history_series) < 3:
        return None

    rendered_series: list[str] = []
    for history in history_series[-3:]:
        if not isinstance(history, (list, tuple)):
            continue
        counts = [count for count in history if isinstance(count, int)]
        if len(counts) < 3:
            continue
        rendered_series.append("->".join(str(count) for count in counts[-3:]))
    if len(rendered_series) < 3:
        return None
    return "3-sprint completion " + " | ".join(rendered_series) + " done"


def _format_sprint_throughput_trend_summary(recent_signals: tuple[Signal, ...]) -> str | None:
    rates: list[float] = []
    for signal in recent_signals:
        rate = (signal.metadata or {}).get("observed_completion_per_business_day")
        if isinstance(rate, (int, float)):
            rates.append(float(rate))
    if len(rates) < 3:
        return None

    deltas = [current - previous for previous, current in zip(rates, rates[1:])]
    total_delta = rates[-1] - rates[0]
    if all(delta > 0.0 for delta in deltas):
        return f"throughput trend up {total_delta:.1f}/day over {len(rates)} sprints"
    if all(delta < 0.0 for delta in deltas):
        return f"throughput trend down {abs(total_delta):.1f}/day over {len(rates)} sprints"
    if all(delta == 0.0 for delta in deltas):
        return f"throughput trend flat over {len(rates)} sprints"
    return None


def _format_snapshot_backed_sprint_throughput_trend_summary(signal: Signal) -> str | None:
    metadata = signal.metadata or {}
    direction = metadata.get("three_iteration_throughput_trend_direction")
    delta = metadata.get("three_iteration_throughput_trend_delta_per_business_day")
    if direction == "flat":
        return "throughput trend flat over 3 sprints"
    if direction not in {"up", "down"} or not isinstance(delta, (int, float)):
        return None
    return f"throughput trend {direction} {abs(float(delta)):.1f}/day over 3 sprints"


def _format_snapshot_backed_historical_sprint_throughput_history_summary(signal: Signal) -> str | None:
    metadata = signal.metadata or {}
    window_count = metadata.get("historical_iteration_window_count")
    history = metadata.get("historical_completion_per_business_day_history")
    if not isinstance(window_count, int) or window_count <= 3:
        return None
    if not isinstance(history, (list, tuple)) or len(history) < window_count:
        return None

    rates = [float(rate) for rate in history if isinstance(rate, (int, float))]
    if len(rates) < window_count:
        return None
    return f"{window_count}-sprint throughput " + "->".join(f"{rate:.1f}" for rate in rates[-window_count:]) + "/day"


def _format_snapshot_backed_historical_sprint_throughput_trend_summary(signal: Signal) -> str | None:
    metadata = signal.metadata or {}
    window_count = metadata.get("historical_iteration_window_count")
    direction = metadata.get("historical_throughput_trend_direction")
    delta = metadata.get("historical_throughput_trend_delta_per_business_day")
    if not isinstance(window_count, int) or window_count <= 3:
        return None
    if direction == "flat":
        return f"historical throughput trend flat over {window_count} sprints"
    if direction not in {"up", "down"} or not isinstance(delta, (int, float)):
        return None
    return f"historical throughput trend {direction} {abs(float(delta)):.1f}/day over {window_count} sprints"


def _format_sprint_capacity_utilization_average_summary(recent_signals: tuple[Signal, ...]) -> str | None:
    utilizations: list[int] = []
    for signal in recent_signals:
        utilization = _calculate_capacity_utilization_pct(signal)
        if utilization is not None:
            utilizations.append(utilization)
    if len(utilizations) < 3:
        return None

    average_utilization = round(sum(utilizations) / len(utilizations))
    return f"{len(utilizations)}-sprint cap util avg {average_utilization}%"


def _format_sprint_open_average_summary(recent_signals: tuple[Signal, ...]) -> str | None:
    open_counts: list[int] = []
    for signal in recent_signals:
        open_item_count = (signal.metadata or {}).get("open_item_count")
        if isinstance(open_item_count, int):
            open_counts.append(open_item_count)
    if len(open_counts) < 3:
        return None

    average_open_count = round(sum(open_counts) / len(open_counts))
    return f"{len(open_counts)}-sprint open avg {average_open_count}"


def _format_snapshot_backed_sprint_open_average_summary(signal: Signal) -> str | None:
    average_open_count = (signal.metadata or {}).get("three_iteration_average_open_item_count")
    if not isinstance(average_open_count, int):
        return None
    return f"3-sprint open avg {average_open_count}"


def _format_snapshot_backed_sprint_open_history_summary(signal: Signal) -> str | None:
    history = (signal.metadata or {}).get("three_iteration_open_item_count_history")
    if not isinstance(history, (list, tuple)) or len(history) < 3:
        return None

    open_counts = [count for count in history if isinstance(count, int)]
    if len(open_counts) < 3:
        return None
    return "3-sprint open " + "->".join(str(count) for count in open_counts[-3:])


def _format_snapshot_backed_historical_sprint_open_history_summary(signal: Signal) -> str | None:
    metadata = signal.metadata or {}
    window_count = metadata.get("historical_iteration_window_count")
    history = metadata.get("historical_open_item_count_history")
    if not isinstance(window_count, int) or window_count <= 3:
        return None
    if not isinstance(history, (list, tuple)) or len(history) < window_count:
        return None

    open_counts = [count for count in history if isinstance(count, int)]
    if len(open_counts) < window_count:
        return None
    return f"{window_count}-sprint open " + "->".join(str(count) for count in open_counts[-window_count:])


def _format_snapshot_backed_historical_sprint_burndown_history_series_summary(signal: Signal) -> str | None:
    metadata = signal.metadata or {}
    window_count = metadata.get("historical_iteration_window_count")
    history_series = metadata.get("historical_open_history_series")
    if not isinstance(window_count, int) or window_count <= 3:
        return None
    if not isinstance(history_series, (list, tuple)) or len(history_series) < window_count:
        return None

    rendered_series: list[str] = []
    for history in history_series[-window_count:]:
        if not isinstance(history, (list, tuple)):
            continue
        counts = [count for count in history if isinstance(count, int)]
        if len(counts) < 3:
            continue
        rendered_series.append("->".join(str(count) for count in counts[-3:]))
    if len(rendered_series) < window_count:
        return None
    return f"{window_count}-sprint burndown " + " | ".join(rendered_series) + " open"


def _format_snapshot_backed_sprint_burndown_history_series_summary(signal: Signal) -> str | None:
    history_series = (signal.metadata or {}).get("three_iteration_open_history_series")
    if not isinstance(history_series, (list, tuple)) or len(history_series) < 3:
        return None

    rendered_series: list[str] = []
    for history in history_series[-3:]:
        if not isinstance(history, (list, tuple)):
            continue
        counts = [count for count in history if isinstance(count, int)]
        if len(counts) < 3:
            continue
        rendered_series.append("->".join(str(count) for count in counts[-3:]))
    if len(rendered_series) < 3:
        return None
    return "3-sprint burndown " + " | ".join(rendered_series) + " open"


def _format_snapshot_backed_historical_sprint_completed_history_series_summary(signal: Signal) -> str | None:
    metadata = signal.metadata or {}
    window_count = metadata.get("historical_iteration_window_count")
    history_series = metadata.get("historical_completed_history_series")
    if not isinstance(window_count, int) or window_count <= 3:
        return None
    if not isinstance(history_series, (list, tuple)) or len(history_series) < window_count:
        return None

    rendered_series: list[str] = []
    for history in history_series[-window_count:]:
        if not isinstance(history, (list, tuple)):
            continue
        counts = [count for count in history if isinstance(count, int)]
        if len(counts) < 3:
            continue
        rendered_series.append("->".join(str(count) for count in counts[-3:]))
    if len(rendered_series) < window_count:
        return None
    return f"{window_count}-sprint completion " + " | ".join(rendered_series) + " done"


def _format_sprint_open_trend_summary(recent_signals: tuple[Signal, ...]) -> str | None:
    open_counts: list[int] = []
    for signal in recent_signals:
        open_item_count = (signal.metadata or {}).get("open_item_count")
        if isinstance(open_item_count, int):
            open_counts.append(open_item_count)
    if len(open_counts) < 3:
        return None

    deltas = [current - previous for previous, current in zip(open_counts, open_counts[1:])]
    total_delta = open_counts[-1] - open_counts[0]
    if all(delta < 0 for delta in deltas):
        return f"open trend down {abs(total_delta)} over {len(open_counts)} sprints"
    if all(delta > 0 for delta in deltas):
        return f"open trend up {total_delta} over {len(open_counts)} sprints"
    if all(delta == 0 for delta in deltas):
        return f"open trend flat over {len(open_counts)} sprints"
    return None


def _format_snapshot_backed_sprint_open_trend_summary(signal: Signal) -> str | None:
    metadata = signal.metadata or {}
    direction = metadata.get("three_iteration_open_trend_direction")
    delta = metadata.get("three_iteration_open_trend_delta_count")
    if direction == "flat":
        return "open trend flat over 3 sprints"
    if direction not in {"up", "down"} or not isinstance(delta, int):
        return None
    return f"open trend {direction} {abs(delta)} over 3 sprints"


def _format_snapshot_backed_historical_sprint_open_trend_summary(signal: Signal) -> str | None:
    metadata = signal.metadata or {}
    window_count = metadata.get("historical_iteration_window_count")
    direction = metadata.get("historical_open_trend_direction")
    delta = metadata.get("historical_open_trend_delta_count")
    if not isinstance(window_count, int) or window_count <= 3:
        return None
    if direction == "flat":
        return f"historical open trend flat over {window_count} sprints"
    if direction not in {"up", "down"} or not isinstance(delta, int):
        return None
    return f"historical open trend {direction} {abs(delta)} over {window_count} sprints"