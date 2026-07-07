from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from src.commands.doctor_checks.models import ADOProbeResult, DoctorCheck, DoctorReport


def run_default_doctor_report(
    *,
    edition_name: str,
    reports_root: Path,
    editions_root: Path,
    programs_root: Path,
    archive_root: Path,
    templates_root: Path,
    fix: bool,
    ado_probe: Callable[..., ADOProbeResult] | None,
    load_bundle_fn: Callable[..., Any],
    validate_slice_contracts_fn: Callable[[Any], Any],
    run_id_doctor: Callable[..., DoctorReport],
    probe_ado_access_fn: Callable[[Any], ADOProbeResult],
    token_check_fn: Callable[[ADOProbeResult], DoctorCheck],
    mail_preview_check_fn: Callable[[], DoctorCheck],
    resolve_edition_fn: Callable[..., Any],
    template_contract_edition_check_fn: Callable[..., DoctorCheck],
    config_governance_check_fn: Callable[..., DoctorCheck],
    latest_gather_integration_check_fn: Callable[..., DoctorCheck | None],
    slice_telemetry_runtime_check_fn: Callable[..., DoctorCheck | None],
    capability_review_check_fn: Callable[..., DoctorCheck | None],
    hygiene_nudge_check_fn: Callable[..., DoctorCheck | None],
    audit_hygiene_check_fn: Callable[..., DoctorCheck | None],
    read_archive_index_fn: Callable[..., Any],
    get_archive_root_fn: Callable[..., Path],
    latest_snapshot_check_fn: Callable[..., DoctorCheck],
    semantic_index_enabled_fn: Callable[[dict[str, Any] | None], bool],
    build_semantic_index_checks_fn: Callable[..., tuple[DoctorCheck, ...]],
    load_overrides_fn: Callable[..., Any],
    seed_overrides_fn: Callable[..., Any],
    template_check_fn: Callable[[Path], DoctorCheck],
    recurring_gate_failures_check_fn: Callable[..., DoctorCheck | None],
    override_streak_check_fn: Callable[..., DoctorCheck | None],
    candidate_queue_backlog_check_fn: Callable[..., DoctorCheck | None],
    claim_freshness_check_fn: Callable[..., DoctorCheck | None],
    coverage_range_check_fn: Callable[..., DoctorCheck | None],
    degraded_confirm_check_fn: Callable[..., DoctorCheck | None],
    ledger_health_check_fn: Callable[..., DoctorCheck | None],
    external_dependencies_check_fn: Callable[..., DoctorCheck | None],
    directory_size_fn: Callable[[Path], int],
    format_bytes_fn: Callable[[int], str],
    default_banned_phrases: tuple[str, ...],
    runtime_layout_check_fn: Callable[[str, Path], DoctorCheck] | None = None,
) -> DoctorReport:
    checks: list[DoctorCheck] = []
    bundle = load_bundle_fn(
        edition_name,
        reports_root=reports_root,
        editions_root=editions_root,
        programs_root=programs_root,
    )
    checks.append(DoctorCheck("Config", "ok", f"reports/{edition_name}/config.yaml valid (schema {bundle.config.schema_version})"))

    if bundle.program_context is not None:
        checks.append(
            DoctorCheck(
                "Context",
                "ok",
                f"program_context.yaml loaded ({len(bundle.program_context.workstreams)} workstreams, {len(bundle.program_context.people)} people)",
            )
        )
    else:
        checks.append(DoctorCheck("Context", "warn", "program_context.yaml is missing."))

    slice_summary = validate_slice_contracts_fn(bundle.slice_contracts)
    if slice_summary.failure_count:
        detail = "; ".join(slice_summary.failures[:2])
        checks.append(DoctorCheck("Slices", "fail", f"{slice_summary.slice_count} slice contracts loaded. {detail}"))
    elif slice_summary.warning_count:
        detail = "; ".join(slice_summary.warnings[:2])
        checks.append(DoctorCheck("Slices", "warn", f"{slice_summary.slice_count} slice contracts loaded. {detail}"))
    else:
        checks.append(DoctorCheck("Slices", "ok", f"{slice_summary.slice_count} slice contracts loaded; contract coverage complete."))
    checks.extend(
        run_id_doctor(
            edition_name=edition_name,
            reports_root=reports_root,
            editions_root=editions_root,
            programs_root=programs_root,
        ).checks
    )

    probe_result = (ado_probe or probe_ado_access_fn)(bundle)
    checks.append(
        DoctorCheck(
            "ADO Access",
            "ok" if probe_result.reachable else "fail",
            probe_result.detail,
        )
    )
    checks.append(token_check_fn(probe_result))
    checks.append(mail_preview_check_fn())
    resolved = resolve_edition_fn(
        edition_name,
        editions_root=editions_root,
        programs_root=programs_root,
    )
    checks.append(
        template_contract_edition_check_fn(
            bundle,
            edition_name=edition_name,
            program_id=resolved.program.id if resolved is not None else None,
            programs_root=programs_root,
        )
    )
    if resolved is not None:
        checks.append(
            config_governance_check_fn(
                edition_name=edition_name,
                resolved=resolved,
                editions_root=editions_root,
                programs_root=programs_root,
            )
        )
    gather_integration_check = latest_gather_integration_check_fn(
        resolved.program.id if resolved is not None else None,
        programs_root,
    )
    if gather_integration_check is not None:
        checks.append(gather_integration_check)
    slice_telemetry_check = slice_telemetry_runtime_check_fn(
        bundle.slice_contracts,
        resolved.program.id if resolved is not None else None,
        programs_root,
    )
    if slice_telemetry_check is not None:
        checks.append(slice_telemetry_check)
    capability_review_check = capability_review_check_fn(
        resolved.program.id if resolved is not None else None,
        programs_root,
    )
    if capability_review_check is not None:
        checks.append(capability_review_check)
    hygiene_nudge_check = hygiene_nudge_check_fn(resolved=resolved, programs_root=programs_root)
    if hygiene_nudge_check is not None:
        checks.append(hygiene_nudge_check)
    audit_hygiene_check = audit_hygiene_check_fn(
        program_id=resolved.program.id if resolved is not None else None,
        raw_program=resolved.raw_program if resolved is not None else None,
        programs_root=programs_root,
    )
    if audit_hygiene_check is not None:
        checks.append(audit_hygiene_check)

    archive_index = read_archive_index_fn(edition_name, archive_root=archive_root)
    archive_root_path = get_archive_root_fn(edition_name, archive_root)
    missing_archive_paths = [
        entry.snapshot_path
        for entry in archive_index.issues
        if entry.snapshot_path is not None and not Path(entry.snapshot_path).exists()
    ]
    if missing_archive_paths:
        checks.append(DoctorCheck("Archive", "fail", f"index.json references missing files ({len(missing_archive_paths)} missing)."))
    else:
        checks.append(DoctorCheck("Archive", "ok", f"archive/{edition_name}/ - {len(archive_index.issues)} issues, index intact"))

    checks.append(latest_snapshot_check_fn(edition_name, archive_root_path, archive_index))

    if resolved is not None and semantic_index_enabled_fn(resolved.raw_program):
        checks.extend(build_semantic_index_checks_fn(edition_name=edition_name, archive_root=archive_root))

    overrides_document = load_overrides_fn(edition_name, reports_root=reports_root)
    if overrides_document is not None:
        checks.append(DoctorCheck("Overrides", "ok", "overrides.yaml present, valid YAML"))
    elif fix:
        seeded = seed_overrides_fn(edition_name, bundle, reports_root)
        checks.append(DoctorCheck("Overrides", "warn", f"overrides.yaml was missing and was created at {seeded}"))
    else:
        checks.append(DoctorCheck("Overrides", "warn", "overrides.yaml is missing."))

    custom_phrases = tuple(
        phrase for phrase in bundle.editorial_rules.banned_phrases if phrase.lower() not in {item.lower() for item in default_banned_phrases}
    )
    if custom_phrases:
        checks.append(DoctorCheck("Editorial", "warn", f"editorial_rules.yaml: {len(custom_phrases)} custom banned phrases added"))
    else:
        checks.append(DoctorCheck("Editorial", "ok", "editorial_rules.yaml loaded"))

    checks.append(template_check_fn(templates_root))
    checks.append(DoctorCheck("Disk", "ok", f"Archive size: {format_bytes_fn(directory_size_fn(archive_root_path))}"))

    if runtime_layout_check_fn is not None and resolved is not None:
        checks.append(runtime_layout_check_fn(resolved.program.id, programs_root))

    gate_failure_check = recurring_gate_failures_check_fn(
        resolved.program.id if resolved is not None else None,
        programs_root,
    )
    if gate_failure_check is not None:
        checks.append(gate_failure_check)
    override_streak_check = override_streak_check_fn(
        resolved.program.id if resolved is not None else None,
        programs_root,
    )
    if override_streak_check is not None:
        checks.append(override_streak_check)
    candidate_backlog_check = candidate_queue_backlog_check_fn(
        resolved.program.id if resolved is not None else None,
        programs_root,
    )
    if candidate_backlog_check is not None:
        checks.append(candidate_backlog_check)
    claim_freshness_check = claim_freshness_check_fn(
        resolved.program.id if resolved is not None else None,
        edition_name,
        archive_root,
        programs_root,
    )
    if claim_freshness_check is not None:
        checks.append(claim_freshness_check)
    coverage_range_check = coverage_range_check_fn(
        resolved.program.id if resolved is not None else None,
        resolved.raw_program if resolved is not None else None,
        programs_root,
    )
    if coverage_range_check is not None:
        checks.append(coverage_range_check)
    degraded_confirm_check = degraded_confirm_check_fn(
        resolved.program.id if resolved is not None else None,
        edition_name,
        archive_root,
        programs_root,
    )
    if degraded_confirm_check is not None:
        checks.append(degraded_confirm_check)
    ledger_health_check = ledger_health_check_fn(
        resolved.program.id if resolved is not None else None,
        programs_root,
    )
    if ledger_health_check is not None:
        checks.append(ledger_health_check)
    external_dep_check = external_dependencies_check_fn(
        resolved.program.id if resolved is not None else None,
        programs_root,
    )
    if external_dep_check is not None:
        checks.append(external_dep_check)

    return DoctorReport(edition=edition_name, checks=tuple(checks))
