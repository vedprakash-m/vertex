from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sqlite3
from typing import Any

from src.commands.channel_wiring import resolve_channel_config
from src.core.channel_registry_store import ChannelRegistryStore
from src.core.program_paths import resolve_channel_registry_path_for_read
from src.core.edition_resolver import _parse_program
from src.core.gather_channel_support import config_provider_instance_id
from src.core.models_v2 import IntegrationError, Signal, Workstream
from src.core.uil_channel_flags import UIL_CHANNEL_ENV_FLAGS, uil_ado_enabled, uil_channel_enabled
from src.core.yaml_utils import load_yaml_mapping


def build_gather_channel_states(
    *,
    program_id: str,
    programs_root: Path,
    workstreams: tuple[Workstream, ...],
    ado_signals: tuple[Signal, ...],
    kusto_signals: tuple[Signal, ...],
    workiq_signals: tuple[Signal, ...],
    icm_signals: tuple[Signal, ...],
    gather_flags: dict[str, bool],
    previous_channels: dict[str, dict[str, Any]] | None = None,
    integration_error_details: tuple[IntegrationError, ...] = (),
    format_optional_datetime,
) -> dict[str, dict[str, Any]]:
    transcript_series_total, transcript_series_id_null = count_transcript_series_state(workstreams)
    workiq_message_count = count_signals_for_sources(workiq_signals, {"workiq/email", "workiq/teams"})
    transcript_count = count_signals_for_sources(workiq_signals, {"workiq/transcript"})
    latest_errors_by_source = latest_channel_errors_by_source(integration_error_details)
    states = {
        "ado": build_channel_state(
            active=True,
            signal_count=count_signals_for_sources(
                ado_signals,
                {"ado/revision", "ado/odata", "ado/comment", "vertex/freshness"},
            ),
            expected_min=1,
            last_error=latest_errors_by_source.get("ado"),
        ),
        "kusto": build_channel_state(
            active=gather_flags.get("kusto", False),
            signal_count=count_signals_for_sources(kusto_signals, {"kusto", "kusto_kpi"}),
            expected_min=10,
            reason_not_active=None if gather_flags.get("kusto", False) else "flag_not_passed",
            last_error=latest_errors_by_source.get("kusto"),
        ),
        "workiq": {
            **build_channel_state(
                active=gather_flags.get("workiq", False),
                signal_count=workiq_message_count,
                expected_min=8,
                reason_not_active=None if gather_flags.get("workiq", False) else "flag_not_passed",
                last_error=latest_errors_by_source.get("workiq"),
            ),
            "email_signals": count_signals_for_sources(workiq_signals, {"workiq/email"}),
            "teams_signals": count_signals_for_sources(workiq_signals, {"workiq/teams"}),
        },
        "transcript": {
            **build_channel_state(
                active=gather_flags.get("workiq", False),
                signal_count=transcript_count,
                expected_min=2,
                reason_not_active=None if gather_flags.get("workiq", False) else "flag_not_passed",
                last_error=latest_errors_by_source.get("workiq"),
            ),
            "configured_series": transcript_series_total,
            "series_id_null": transcript_series_id_null,
            "all_series_ids_present": transcript_series_id_null == 0,
            "meets_expected_min": bool(
                gather_flags.get("workiq", False) and transcript_count >= 2 and transcript_series_id_null == 0
            )
            if gather_flags.get("workiq", False)
            else False,
        },
        "icm": build_channel_state(
            active=gather_flags.get("icm", False),
            signal_count=count_signals_for_sources(icm_signals, {"icm"}),
            expected_min=0,
            reason_not_active=None if gather_flags.get("icm", False) else "flag_not_passed",
            last_error=latest_errors_by_source.get("icm"),
        ),
    }
    for uil_channel in UIL_CHANNEL_ENV_FLAGS:
        if uil_channel not in states:
            states[uil_channel] = {}
        states[uil_channel].update(
            build_uil_channel_state(
                program_id,
                uil_channel,
                enabled=uil_channel_enabled(uil_channel),
                programs_root=programs_root,
                format_optional_datetime=format_optional_datetime,
            )
        )
    preserve_uil_channel_state(states, previous_channels)
    return states


def build_uil_ado_channel_state(
    program_id: str,
    *,
    programs_root: Path,
    format_optional_datetime,
) -> dict[str, Any]:
    return build_uil_channel_state(
        program_id,
        "ado",
        enabled=uil_ado_enabled(),
        programs_root=programs_root,
        format_optional_datetime=format_optional_datetime,
    )


def build_uil_channel_state(
    program_id: str,
    channel: str,
    *,
    enabled: bool,
    programs_root: Path,
    format_optional_datetime,
) -> dict[str, Any]:
    registry_path = resolve_channel_registry_path_for_read(program_id, programs_root=programs_root)
    config = configured_channel_config(program_id, channel, programs_root=programs_root)
    uil_enabled = bool(enabled and config is not None and config.enabled)
    provider_instance_id = config_provider_instance_id(config) if config is not None else None
    state: dict[str, Any] = {
        "uil_enabled": uil_enabled,
        "uil_registry_file_present": registry_path.exists(),
    }
    if not registry_path.exists():
        if uil_enabled:
            state["uil_health"] = "missing_registry"
        return state
    if config is None or not config.enabled:
        return state
    try:
        store = ChannelRegistryStore(registry_path, program_id)
        last_delta = next(iter(store.recent_deltas(channel, limit=1, provider_instance_id=provider_instance_id)), None)
        state.update(
            {
                "uil_health": "ok",
                "uil_registry_size": store.registration_count(channel, provider_instance_id=provider_instance_id),
                "uil_last_discovery_at": format_optional_datetime(
                    store.last_discovery_at(channel, provider_instance_id=provider_instance_id)
                ),
                "uil_last_delta_summary": last_delta.summary if last_delta is not None else None,
                "uil_last_delta_shrinkage_pct": last_delta.shrinkage_pct if last_delta is not None else None,
                "uil_last_delta_computed_at": format_optional_datetime(last_delta.computed_at) if last_delta is not None else None,
                "uil_discovery_completeness": last_delta.completeness.value if last_delta is not None else None,
                "uil_scope_health": store.recent_scope_health(channel, provider_instance_id=provider_instance_id),
            }
        )
    except (sqlite3.Error, OSError, ValueError) as error:
        state.update(
            {
                "uil_health": "error",
                "uil_error": str(error),
            }
        )
    return state


def configured_channel_config(
    program_id: str,
    channel: str,
    *,
    programs_root: Path,
) -> Any | None:
    program_path = programs_root / program_id / "program.yaml"
    if not program_path.exists():
        return None
    program = _parse_program(load_yaml_mapping(program_path), program_path)
    return resolve_channel_config(program, channel, programs_root=programs_root)


def preserve_uil_channel_state(
    states: dict[str, dict[str, Any]],
    previous_channels: dict[str, dict[str, Any]] | None,
) -> None:
    if not previous_channels:
        return
    for channel_name, entry in states.items():
        if not bool(entry.get("uil_enabled")):
            continue
        previous_entry = previous_channels.get(channel_name)
        if not isinstance(previous_entry, dict):
            continue
        has_current_health = any(
            key in entry
            for key in (
                "uil_health",
                "uil_registry_size",
                "uil_last_discovery_at",
                "uil_last_delta_summary",
            )
        )
        for key, value in previous_entry.items():
            if not key.startswith("uil_"):
                continue
            if key not in entry or not has_current_health:
                entry[key] = value


def latest_channel_errors_by_source(
    integration_error_details: tuple[IntegrationError, ...],
) -> dict[str, str]:
    latest_errors_by_source: dict[str, str] = {}
    for detail in integration_error_details:
        normalized_source = detail.source.strip().lower()
        if not normalized_source:
            continue
        latest_errors_by_source[normalized_source] = detail.message
    return latest_errors_by_source


def build_channel_state(
    *,
    active: bool,
    signal_count: int,
    expected_min: int,
    failure_mode: str = "degrade",
    reason_not_active: str | None = None,
    last_error: str | None = None,
) -> dict[str, Any]:
    return {
        "active": active,
        "signal_count": signal_count,
        "expected_min": expected_min,
        "failure_mode": failure_mode,
        "meets_expected_min": active and signal_count >= expected_min,
        "reason_not_active": reason_not_active,
        "last_error": last_error,
    }


def count_signals_for_sources(signals: tuple[Signal, ...], allowed_sources: set[str]) -> int:
    return sum(1 for signal in signals if signal.source in allowed_sources)


def count_transcript_series_state(workstreams: tuple[Workstream, ...]) -> tuple[int, int]:
    total = 0
    missing = 0
    for workstream in workstreams:
        signal_sources = workstream.signal_sources
        if signal_sources is None:
            continue
        for series in signal_sources.teams_meeting_series:
            if not series.include_transcripts:
                continue
            total += 1
            if not series.series_id:
                missing += 1
    return total, missing
