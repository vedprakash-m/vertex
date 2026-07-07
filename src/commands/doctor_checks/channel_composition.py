from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from src.commands.doctor_checks.models import DoctorCheck, DoctorReport


def run_channel_doctor(
    *,
    edition_name: str,
    reports_root: Path,
    editions_root: Path,
    programs_root: Path,
    resolve_edition_fn: Callable[..., Any],
    load_gather_state_fn: Callable[..., Any],
    load_bundle_fn: Callable[..., Any],
    current_doctor_kusto_targets_fn: Callable[..., Any],
    channel_last_error_fn: Callable[..., str | None],
    channel_auth_failure_detail_fn: Callable[..., str | None],
    build_m365_registry_review_metadata_fn: Callable[..., dict[str, Any] | None],
    summarize_m365_discovery_fn: Callable[[dict[str, Any]], str],
    summarize_m365_registry_review_fn: Callable[[dict[str, Any]], str],
    slice_source_health_check_fn: Callable[..., DoctorCheck | None],
    load_source_waivers_fn: Callable[..., Any],
    source_health_function_name_for_edition_fn: Callable[[Any], str],
    conversion_fidelity_check_fn: Callable[..., DoctorCheck | None],
    eta_credibility_check_fn: Callable[..., DoctorCheck | None],
    m365_discovery_check_fn: Callable[..., DoctorCheck],
    m365_registry_review_check_fn: Callable[..., DoctorCheck],
    m365_registry_promotion_check_fn: Callable[..., DoctorCheck],
    uil_registry_checks_fn: Callable[[dict[str, dict[str, Any]]], list[DoctorCheck]],
    ado_pr_coverage_check_fn: Callable[..., DoctorCheck],
    channel_delta_check_fn: Callable[..., DoctorCheck],
    channel_detail_check_fn: Callable[..., DoctorCheck],
) -> DoctorReport:
    resolved = resolve_edition_fn(
        edition_name,
        editions_root=editions_root,
        programs_root=programs_root,
    )
    if resolved is None:
        return DoctorReport(edition=edition_name, checks=(DoctorCheck("Channels", "fail", f"Edition '{edition_name}' could not be resolved."),))

    gather_state = load_gather_state_fn(resolved.program.id, programs_root=programs_root)
    if gather_state is None:
        return DoctorReport(
            edition=edition_name,
            checks=(DoctorCheck("Channels", "warn", "No gather state recorded yet."),),
        )
    if not gather_state.channels:
        return DoctorReport(
            edition=edition_name,
            checks=(DoctorCheck("Channels", "warn", "Latest gather state does not include channel telemetry. Re-run gather with the updated client."),),
        )

    bundle = load_bundle_fn(
        edition_name,
        reports_root=reports_root,
        editions_root=editions_root,
        programs_root=programs_root,
    )
    channel_entries = gather_state.channels
    min_completeness_pct = resolved.program.min_channel_completeness_pct
    current_kusto_targets = current_doctor_kusto_targets_fn(
        program_id=resolved.program.id,
        program=resolved.program,
        programs_root=programs_root,
    )
    active_channels = [name for name, entry in channel_entries.items() if bool(entry.get("active"))]
    channels_at_expected_min = [name for name in active_channels if bool(channel_entries[name].get("meets_expected_min"))]
    completeness_pct = int(round((len(channels_at_expected_min) / len(active_channels)) * 100)) if active_channels else 100
    failed_queries = sorted(
        query_id
        for query_id, state in gather_state.query_states.items()
        if bool(state.get("last_cycle_succeeded")) is False
    )
    zero_row_queries = sorted(
        query_id
        for query_id, state in gather_state.query_states.items()
        if bool(state.get("last_cycle_succeeded")) is True
        and int(state.get("row_count") or 0) == 0
        and not bool(state.get("zero_rows_ok"))
    )
    silent_zero_yield_channels = sorted(
        channel_name
        for channel_name, entry in channel_entries.items()
        if channel_name in {"workiq"}
        and bool(entry.get("active"))
        and int(entry.get("expected_min") or 0) > 0
        and int(entry.get("signal_count") or 0) == 0
        and gather_state.integration_errors <= 0
    )
    stale_queries = sorted(
        query_id
        for query_id, state in gather_state.query_states.items()
        if state.get("data_freshness_ok") is False
    )
    frozen_queries = sorted(
        query_id
        for query_id, state in gather_state.query_states.items()
        if bool(state.get("value_frozen_warning"))
    )
    flag_omitted_channels = sorted(
        channel_name
        for channel_name, entry in channel_entries.items()
        if isinstance(entry, dict)
        and channel_name != "ado"
        and str(entry.get("reason_not_active") or "") == "flag_not_passed"
    )
    degraded_channels = sorted(
        channel_name
        for channel_name, entry in channel_entries.items()
        if isinstance(entry, dict)
        and bool(entry.get("active"))
        and channel_last_error_fn(channel_name, entry, current_kusto_targets=current_kusto_targets) is not None
    )
    auth_guidance_by_channel = {
        channel_name: auth_detail
        for channel_name, entry in sorted(channel_entries.items())
        if isinstance(entry, dict)
        for auth_detail in [
            channel_auth_failure_detail_fn(
                channel_name,
                channel_last_error_fn(channel_name, entry, current_kusto_targets=current_kusto_targets),
            )
        ]
        if auth_detail is not None
    }
    m365_registry_review = (
        build_m365_registry_review_metadata_fn(resolved.program.id, programs_root=programs_root)
        if resolved.program.m365 is not None and resolved.program.m365.enabled
        else None
    )
    detail = f"Channel completeness {completeness_pct}% ({len(channels_at_expected_min)}/{len(active_channels)} active channels met expected minimums)."
    if auth_guidance_by_channel:
        guidance_summary = "; ".join(
            f"{channel_name}: {guidance}"
            for channel_name, guidance in auth_guidance_by_channel.items()
        )
        detail = f"{detail} Channel access issues: {guidance_summary}."
    if degraded_channels:
        detail = f"{detail} Active degraded channels: {', '.join(degraded_channels)}."
    if flag_omitted_channels:
        detail = f"{detail} Flag-omitted channels: {', '.join(flag_omitted_channels)}."
    if failed_queries:
        detail = f"{detail} Failed queries: {', '.join(failed_queries)}."
    if silent_zero_yield_channels:
        detail = f"{detail} Silent zero-yield channels: {', '.join(silent_zero_yield_channels)}."
    if zero_row_queries:
        detail = f"{detail} Zero-row queries: {', '.join(zero_row_queries)}."
    if stale_queries:
        detail = f"{detail} Stale queries: {', '.join(stale_queries)}."
    if frozen_queries:
        detail = f"{detail} Frozen metric queries: {', '.join(frozen_queries)}."
    if gather_state.m365_discovery:
        discovery_summary = summarize_m365_discovery_fn(gather_state.m365_discovery)
        if discovery_summary:
            detail = f"{detail} {discovery_summary}"
    if m365_registry_review is not None:
        review_summary = summarize_m365_registry_review_fn(m365_registry_review)
        if review_summary:
            detail = f"{detail} {review_summary}"
    checks: list[DoctorCheck] = [
        DoctorCheck(
            "Channels",
            "ok" if completeness_pct >= min_completeness_pct and not degraded_channels and not failed_queries and not stale_queries and not frozen_queries and not (m365_registry_review or {}).get("has_issues") else "warn",
            detail,
            metadata={
                "gathered_at": gather_state.gathered_at.isoformat(),
                "gather_flags": gather_state.gather_flags,
                "channels_active": active_channels,
                "channels_at_expected_min": channels_at_expected_min,
                "completeness_pct": completeness_pct,
                "min_completeness_pct": min_completeness_pct,
                "degraded_channels": degraded_channels,
                "flag_omitted_channels": flag_omitted_channels,
                "auth_guidance_by_channel": auth_guidance_by_channel,
                "failed_queries": failed_queries,
                "silent_zero_yield_channels": silent_zero_yield_channels,
                "zero_row_queries": zero_row_queries,
                "stale_queries": stale_queries,
                "frozen_queries": frozen_queries,
                "m365_discovery": gather_state.m365_discovery,
                "m365_registry_review": m365_registry_review,
            },
        )
    ]
    source_health_check = slice_source_health_check_fn(
        bundle.slice_contracts,
        gather_state,
        load_source_waivers_fn(resolved.program.id, programs_root=programs_root),
        function_name=source_health_function_name_for_edition_fn(bundle.config.edition.type),
    )
    if source_health_check is not None:
        checks.append(source_health_check)
    if resolved.program.id:
        fidelity_check = conversion_fidelity_check_fn(resolved.program.id, programs_root)
        if fidelity_check is not None:
            checks.append(fidelity_check)
    if resolved.program.id:
        credibility_check = eta_credibility_check_fn(resolved.program.id, programs_root)
        if credibility_check is not None:
            checks.append(credibility_check)

    if gather_state.m365_discovery:
        checks.append(m365_discovery_check_fn(gather_state.m365_discovery, previous_entry=gather_state.previous_m365_discovery))
    if m365_registry_review is not None:
        checks.append(m365_registry_review_check_fn(m365_registry_review))
        checks.append(m365_registry_promotion_check_fn(m365_registry_review))
    checks.extend(uil_registry_checks_fn(channel_entries))
    checks.append(ado_pr_coverage_check_fn(resolved.workstreams))
    if gather_state.previous_gathered_at is not None and gather_state.previous_channels:
        checks.append(
            channel_delta_check_fn(
                previous_gathered_at=gather_state.previous_gathered_at,
                current_channels=channel_entries,
                previous_channels=gather_state.previous_channels,
                current_failed_queries=failed_queries,
                previous_query_states=gather_state.previous_query_states,
                current_stale_queries=stale_queries,
                current_frozen_queries=frozen_queries,
                current_m365_discovery=gather_state.m365_discovery,
                previous_m365_discovery=gather_state.previous_m365_discovery,
            )
        )

    for channel_name in ("ado", "kusto", "workiq", "transcript", "icm"):
        entry = channel_entries.get(channel_name)
        if not isinstance(entry, dict):
            continue
        checks.append(channel_detail_check_fn(channel_name, entry, current_kusto_targets=current_kusto_targets))
    # S-12: Teams channel ingestion is a v1 accepted limitation (P3-2a spike unconfirmed).
    checks.append(DoctorCheck(
        "Teams",
        "ok",
        "Teams channel ingestion: v1 accepted limitation. "
        "Teams channel message ingestion is out of scope for v1 (P3-2a spike unconfirmed). "
        "Meeting transcripts are supported via the transcript channel. "
        "Teams channel ingestion planned for Phase 3.",
        metadata={"limitation": "teams_channel_ingestion", "planned_phase": "3", "spike": "P3-2a"},
    ))
    # S-13: Outlook COM source-delivery is a Phase 3 feasibility spike (not a v1 delivery path).
    checks.append(DoctorCheck(
        "Outlook COM",
        "warn",
        "Outlook COM source-delivery: Phase 3 feasibility spike. "
        "Direct Outlook COM automation (win32com / redemption) is not a supported delivery path in v1. "
        "The Graph preview-send channel is the supported v1 delivery path. "
        "Outlook COM is planned as a Phase 3 spike (S-13) for native send scenarios.",
        metadata={"limitation": "outlook_com_source_delivery", "planned_phase": "3", "spike": "S-13"},
    ))
    return DoctorReport(edition=edition_name, checks=tuple(checks))
