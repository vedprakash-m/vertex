from __future__ import annotations

import re
from typing import Any, Callable

from src.commands.doctor_checks.models import DoctorCheck


def uil_registry_checks(channel_entries: dict[str, dict[str, Any]]) -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []
    for channel_name, entry in sorted(channel_entries.items()):
        check = uil_registry_check(channel_name, entry)
        if check is not None:
            checks.append(check)
    return checks


def uil_registry_check(channel_name: str, channel_entry: Any) -> DoctorCheck | None:
    if not isinstance(channel_entry, dict) or not bool(channel_entry.get("uil_enabled")):
        return None
    label = f"UIL {channel_name.upper()}" if len(channel_name) <= 3 else f"UIL {channel_name.title()}"
    health = str(channel_entry.get("uil_health") or "")
    registry_size = int(channel_entry.get("uil_registry_size") or 0)
    last_discovery_at = channel_entry.get("uil_last_discovery_at")
    scope_health_raw = channel_entry.get("uil_scope_health")
    scope_health = scope_health_raw if isinstance(scope_health_raw, dict) else {}
    degraded_scopes = {
        str(scope_id): str(status)
        for scope_id, status in scope_health.items()
        if str(status).strip() and str(status).strip().lower() != "ok"
    }
    if health == "ok" and registry_size > 0 and not degraded_scopes:
        return DoctorCheck(
            label,
            "ok",
            f"{label} registry has {registry_size} active registrations; last discovery {last_discovery_at or 'unknown'}.",
            metadata={
                "registry_size": registry_size,
                "last_discovery_at": last_discovery_at,
                "last_delta_summary": channel_entry.get("uil_last_delta_summary"),
                "last_delta_shrinkage_pct": channel_entry.get("uil_last_delta_shrinkage_pct"),
                "scope_health": scope_health,
            },
        )
    if health == "ok" and registry_size > 0:
        degraded_summary = ", ".join(f"{scope_id}={status}" for scope_id, status in sorted(degraded_scopes.items()))
        return DoctorCheck(
            label,
            "warn",
            f"{label} registry has {registry_size} active registrations, but {len(degraded_scopes)} discovery scope(s) are degraded ({degraded_summary}); last discovery {last_discovery_at or 'unknown'}.",
            metadata={
                "registry_size": registry_size,
                "last_discovery_at": last_discovery_at,
                "last_delta_summary": channel_entry.get("uil_last_delta_summary"),
                "last_delta_shrinkage_pct": channel_entry.get("uil_last_delta_shrinkage_pct"),
                "scope_health": scope_health,
                "degraded_scope_health": degraded_scopes,
            },
        )
    return DoctorCheck(
        label,
        "warn",
        f"{label} is enabled but registry health is {health or 'unknown'} with {registry_size} active registrations.",
        metadata={
            "registry_file_present": channel_entry.get("uil_registry_file_present"),
            "registry_size": registry_size,
            "health": health,
            "error": channel_entry.get("uil_error"),
            "scope_health": scope_health,
        },
    )


def ado_pr_coverage_check(workstreams: tuple[Any, ...]) -> DoctorCheck:
    metadata = {
        "workstream_count": len(workstreams),
        "configured_workstream_ids": [],
        "missing_workstream_ids": [],
        "configured_repository_ids": {},
    }
    if not workstreams:
        return DoctorCheck(
            "ADO PR Coverage",
            "ok",
            "No workstreams are configured for this program.",
            metadata=metadata,
        )

    configured_workstream_ids: list[str] = []
    missing_workstream_ids: list[str] = []
    configured_repository_ids: dict[str, list[str]] = {}
    for workstream in workstreams:
        repository_ids = [repository_id for repository_id in workstream.ado_repository_ids if str(repository_id).strip()]
        if repository_ids:
            configured_workstream_ids.append(workstream.id)
            configured_repository_ids[workstream.id] = repository_ids
        else:
            missing_workstream_ids.append(workstream.id)

    metadata.update(
        {
            "configured_workstream_ids": configured_workstream_ids,
            "missing_workstream_ids": missing_workstream_ids,
            "configured_repository_ids": configured_repository_ids,
        }
    )
    if not configured_workstream_ids:
        return DoctorCheck(
            "ADO PR Coverage",
            "warn",
            "ADO PR telemetry is structurally unconfigured: no workstreams declare ado_repository_ids. Configure repository IDs to enable ado/pr signals and ado_pr-backed KPI queries.",
            metadata=metadata,
        )
    if missing_workstream_ids:
        return DoctorCheck(
            "ADO PR Coverage",
            "warn",
            f"ADO PR telemetry is only partially configured: {len(configured_workstream_ids)}/{len(workstreams)} workstreams declare ado_repository_ids. Missing: {', '.join(missing_workstream_ids)}.",
            metadata=metadata,
        )
    return DoctorCheck(
        "ADO PR Coverage",
        "ok",
        f"ADO PR telemetry is configured for all {len(workstreams)} workstream(s).",
        metadata=metadata,
    )


def channel_detail_check(
    channel_name: str,
    entry: dict[str, Any],
    *,
    current_kusto_targets: tuple[str, ...] = (),
    channel_auth_failure_detail_fn: Callable[[str, str | None], str | None],
) -> DoctorCheck:
    label = f"Channel:{channel_name}"
    active = bool(entry.get("active"))
    signal_count = int(entry.get("signal_count") or 0)
    expected_min = int(entry.get("expected_min") or 0)
    meets_expected_min = bool(entry.get("meets_expected_min"))
    last_error = channel_last_error(channel_name, entry, current_kusto_targets=current_kusto_targets)
    metadata = {key: value for key, value in entry.items()}
    if not active:
        reason = str(entry.get("reason_not_active") or "inactive")
        detail = f"Inactive on the latest gather ({reason})."
        auth_detail = channel_auth_failure_detail_fn(channel_name, last_error)
        if auth_detail:
            detail = f"{detail} {auth_detail}"
        return DoctorCheck(label, "ok", detail, metadata=metadata)
    if channel_name == "transcript" and int(entry.get("series_id_null") or 0) > 0:
        return DoctorCheck(
            label,
            "warn",
            f"Transcript channel active but {int(entry.get('series_id_null') or 0)} of {int(entry.get('configured_series') or 0)} configured meeting series are missing series_id.",
            metadata=metadata,
        )
    if channel_name == "transcript" and int(entry.get("configured_series") or 0) > 0 and signal_count <= 0:
        detail = (
            f"Transcript channel active but yielded 0 signals across "
            f"{int(entry.get('configured_series') or 0)} configured meeting series."
        )
        auth_detail = channel_auth_failure_detail_fn(channel_name, last_error)
        if auth_detail:
            detail = f"{detail} {auth_detail}"
        return DoctorCheck(label, "warn", detail, metadata=metadata)
    if not meets_expected_min:
        detail = f"Active with {signal_count} signal(s); expected at least {expected_min}."
        auth_detail = channel_auth_failure_detail_fn(channel_name, last_error)
        if auth_detail:
            detail = f"{detail} {auth_detail}"
        return DoctorCheck(
            label,
            "warn",
            detail,
            metadata=metadata,
        )
    if last_error is not None:
        detail = f"Active with {signal_count} signal(s); expected minimum {expected_min} met, but latest gather recorded degradation: {last_error}."
        auth_detail = channel_auth_failure_detail_fn(channel_name, last_error)
        if auth_detail:
            detail = f"{detail} {auth_detail}"
        return DoctorCheck(label, "warn", detail, metadata=metadata)
    return DoctorCheck(label, "ok", f"Active with {signal_count} signal(s); expected minimum {expected_min} met.", metadata=metadata)


def channel_last_error(
    channel_name: str,
    entry: dict[str, Any],
    *,
    current_kusto_targets: tuple[str, ...] = (),
) -> str | None:
    raw_last_error = entry.get("last_error")
    if raw_last_error in (None, ""):
        return None
    last_error = str(raw_last_error).strip() or None
    if (
        last_error is not None
        and channel_name == "kusto"
        and current_kusto_targets
        and (failed_target := extract_kusto_failed_target(last_error)) is not None
        and failed_target not in current_kusto_targets
    ):
        return None
    return last_error


def extract_kusto_failed_target(last_error: str) -> str | None:
    match = re.search(r"Kusto pre-flight failed for\s+(\S+/\S+)", last_error)
    if match is None:
        return None
    return match.group(1).rstrip(".,;:").strip()
