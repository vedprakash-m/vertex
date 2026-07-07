from __future__ import annotations

from datetime import datetime
from typing import Any

from src.commands.doctor_checks.models import DoctorCheck


def channel_delta_check(
    *,
    previous_gathered_at: datetime,
    current_channels: dict[str, dict[str, Any]],
    previous_channels: dict[str, dict[str, Any]],
    current_failed_queries: list[str],
    previous_query_states: dict[str, dict[str, Any]],
    current_stale_queries: list[str],
    current_frozen_queries: list[str],
    current_m365_discovery: dict[str, Any],
    previous_m365_discovery: dict[str, Any],
) -> DoctorCheck:
    current_active_channels, current_channels_at_expected_min, current_completeness_pct = channel_health_snapshot(current_channels)
    _, _, previous_completeness_pct = channel_health_snapshot(previous_channels)
    completeness_delta_pct = current_completeness_pct - previous_completeness_pct

    regressed_channels: list[str] = []
    improved_channels: list[str] = []
    channel_signal_deltas: dict[str, int] = {}
    for channel_name, current_entry in current_channels.items():
        previous_entry = previous_channels.get(channel_name)
        if not isinstance(previous_entry, dict):
            continue
        if not bool(current_entry.get("active")) or not bool(previous_entry.get("active")):
            continue
        current_signal_count = int(current_entry.get("signal_count") or 0)
        previous_signal_count = int(previous_entry.get("signal_count") or 0)
        channel_signal_deltas[channel_name] = current_signal_count - previous_signal_count
        current_meets_expected_min = bool(current_entry.get("meets_expected_min"))
        previous_meets_expected_min = bool(previous_entry.get("meets_expected_min"))
        if previous_meets_expected_min and not current_meets_expected_min:
            regressed_channels.append(channel_name)
        elif not previous_meets_expected_min and current_meets_expected_min:
            improved_channels.append(channel_name)

    previous_failed_queries = sorted(
        query_id
        for query_id, state in previous_query_states.items()
        if bool(state.get("last_cycle_succeeded")) is False
    )
    previous_stale_queries = sorted(
        query_id
        for query_id, state in previous_query_states.items()
        if state.get("data_freshness_ok") is False
    )
    previous_frozen_queries = sorted(
        query_id
        for query_id, state in previous_query_states.items()
        if bool(state.get("value_frozen_warning"))
    )
    newly_failed_queries = sorted(set(current_failed_queries) - set(previous_failed_queries))
    newly_stale_queries = sorted(set(current_stale_queries) - set(previous_stale_queries))
    newly_frozen_queries = sorted(set(current_frozen_queries) - set(previous_frozen_queries))

    m365_signals_without_workstream_delta = int(current_m365_discovery.get("signals_without_workstream") or 0) - int(previous_m365_discovery.get("signals_without_workstream") or 0)
    m365_untracked_threads_delta = int(current_m365_discovery.get("untracked_observed_thread_ids") or 0) - int(previous_m365_discovery.get("untracked_observed_thread_ids") or 0)

    detail = (
        f"Previous run completeness {previous_completeness_pct}% -> current {current_completeness_pct}% "
        f"({completeness_delta_pct:+d} points) from {previous_gathered_at.isoformat()}."
    )
    if regressed_channels:
        detail = f"{detail} Regressed channels: {', '.join(regressed_channels)}."
    if improved_channels:
        detail = f"{detail} Improved channels: {', '.join(improved_channels)}."
    if newly_failed_queries:
        detail = f"{detail} Newly failed queries: {', '.join(newly_failed_queries)}."
    if newly_stale_queries:
        detail = f"{detail} Newly stale queries: {', '.join(newly_stale_queries)}."
    if newly_frozen_queries:
        detail = f"{detail} Newly frozen metric queries: {', '.join(newly_frozen_queries)}."
    if m365_untracked_threads_delta > 0:
        detail = f"{detail} M365 untracked threads increased by {m365_untracked_threads_delta}."
    if m365_signals_without_workstream_delta > 0:
        detail = f"{detail} M365 unattributed signals increased by {m365_signals_without_workstream_delta}."

    has_regression = bool(
        completeness_delta_pct < 0
        or regressed_channels
        or newly_failed_queries
        or newly_stale_queries
        or newly_frozen_queries
        or m365_untracked_threads_delta > 0
        or m365_signals_without_workstream_delta > 0
    )
    return DoctorCheck(
        "Channels Delta",
        "warn" if has_regression else "ok",
        detail,
        metadata={
            "previous_gathered_at": previous_gathered_at.isoformat(),
            "previous_completeness_pct": previous_completeness_pct,
            "current_active_channels": current_active_channels,
            "current_channels_at_expected_min": current_channels_at_expected_min,
            "current_completeness_pct": current_completeness_pct,
            "completeness_delta_pct": completeness_delta_pct,
            "regressed_channels": regressed_channels,
            "improved_channels": improved_channels,
            "channel_signal_deltas": channel_signal_deltas,
            "newly_failed_queries": newly_failed_queries,
            "newly_stale_queries": newly_stale_queries,
            "newly_frozen_queries": newly_frozen_queries,
            "m365_untracked_threads_delta": m365_untracked_threads_delta,
            "m365_signals_without_workstream_delta": m365_signals_without_workstream_delta,
        },
    )


def channel_health_snapshot(channel_entries: dict[str, dict[str, Any]]) -> tuple[list[str], list[str], int]:
    active_channels = [name for name, entry in channel_entries.items() if bool(entry.get("active"))]
    channels_at_expected_min = [name for name in active_channels if bool(channel_entries[name].get("meets_expected_min"))]
    completeness_pct = int(round((len(channels_at_expected_min) / len(active_channels)) * 100)) if active_channels else 100
    return active_channels, channels_at_expected_min, completeness_pct
