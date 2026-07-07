from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from src.commands.doctor_checks.models import ADOProbeResult, DoctorCheck, DoctorReport
from src.core.metric_models import MetricDefinition
from src.core.models_v2 import KustoQuery
from src.m365.agency_bridge import AgencyCapabilities


def run_operator_gates_doctor(
    *,
    edition_name: str,
    reports_root: Path,
    archive_root: Path,
    editions_root: Path,
    programs_root: Path,
    ado_probe: Callable[..., ADOProbeResult] | None,
    kusto_probe: Callable[[KustoQuery], None] | None,
    metric_binding_probe: Any,
    metric_definitions: dict[str, MetricDefinition] | None,
    reality_db_root: Path | None,
    now: datetime | None,
    resolve_edition_fn: Callable[..., Any],
    run_auth_doctor: Callable[..., DoctorReport],
    run_channel_doctor: Callable[..., DoctorReport],
    run_metric_binding_doctor: Callable[..., DoctorReport],
    run_checkpoint_doctor: Callable[..., DoctorReport],
    build_m365_registry_review_metadata: Callable[..., dict[str, Any] | None],
    load_gather_state_fn: Callable[..., Any],
    agency_bridge_factory: Callable[[], Any],
    operator_gate_m365_ids_check: Callable[..., DoctorCheck],
    operator_gate_transcript_health_check: Callable[..., DoctorCheck],
    operator_gate_kusto_validation_check: Callable[..., DoctorCheck],
    operator_gate_checkpoint_creation_check: Callable[..., DoctorCheck],
    operator_gate_rollback_drill_check: Callable[..., DoctorCheck],
) -> DoctorReport:
    resolved = resolve_edition_fn(
        edition_name,
        editions_root=editions_root,
        programs_root=programs_root,
    )
    if resolved is None:
        return DoctorReport(edition=edition_name, checks=(DoctorCheck("Operator Gates", "fail", f"Edition '{edition_name}' could not be resolved."),))

    auth_report = run_auth_doctor(
        edition_name=edition_name,
        reports_root=reports_root,
        editions_root=editions_root,
        programs_root=programs_root,
        ado_probe=ado_probe,
        kusto_probe=kusto_probe,
    )
    channel_report = run_channel_doctor(
        edition_name=edition_name,
        reports_root=reports_root,
        editions_root=editions_root,
        programs_root=programs_root,
    )
    metric_report = run_metric_binding_doctor(
        edition_name=edition_name,
        editions_root=editions_root,
        programs_root=programs_root,
        reality_db_root=reality_db_root,
        metric_binding_probe=metric_binding_probe,
        metric_definitions=metric_definitions,
        now=now,
    )
    checkpoint_report = run_checkpoint_doctor(
        edition_name=edition_name,
        editions_root=editions_root,
        programs_root=programs_root,
        archive_root=archive_root,
    )

    auth_checks = {check.label: check for check in auth_report.checks}
    channel_checks = {check.label: check for check in channel_report.checks}
    metric_checks = {check.label: check for check in metric_report.checks}
    checkpoint_checks = {check.label: check for check in checkpoint_report.checks}

    m365_review = (
        build_m365_registry_review_metadata(resolved.program.id, programs_root=programs_root)
        if resolved.program.m365 is not None and resolved.program.m365.enabled
        else None
    )
    gather_state = load_gather_state_fn(resolved.program.id, programs_root=programs_root)
    agency_caps = agency_bridge_factory().probe() if m365_review is not None else AgencyCapabilities()

    gate_checks = [
        operator_gate_m365_ids_check(
            program_id=resolved.program.id,
            programs_root=programs_root,
            edition_name=edition_name,
            registry_review=m365_review,
            m365_discovery=gather_state.m365_discovery if gather_state is not None else None,
            agency_caps=agency_caps,
        ),
        operator_gate_transcript_health_check(
            edition_name=edition_name,
            transcript_check=channel_checks.get("Channel:transcript"),
            source_health_check=channel_checks.get("Source Health"),
        ),
        operator_gate_kusto_validation_check(
            edition_name=edition_name,
            kusto_access_check=auth_checks.get("Kusto Access"),
            kusto_validation_check=auth_checks.get("Kusto Validation"),
            metric_bindings_check=metric_checks.get("Metric Bindings"),
            metric_rollout_check=metric_checks.get("Metric Rollout"),
        ),
        operator_gate_checkpoint_creation_check(
            edition_name=edition_name,
            checkpoint_inventory_check=checkpoint_checks.get("Checkpoint Inventory"),
        ),
        operator_gate_rollback_drill_check(
            edition_name=edition_name,
            checkpoint_inventory_check=checkpoint_checks.get("Checkpoint Inventory"),
            checkpoint_coverage_check=checkpoint_checks.get("Checkpoint Coverage"),
            editions_root=editions_root,
            programs_root=programs_root,
        ),
    ]
    blocking_gate_labels = [check.label for check in gate_checks if check.status == "fail"]
    warning_gate_labels = [check.label for check in gate_checks if check.status == "warn"]
    if blocking_gate_labels:
        summary_status = "fail"
        summary_detail = (
            f"{len(blocking_gate_labels)} blocking operator gate(s) remain: {', '.join(blocking_gate_labels)}. "
            "Execute them in order so durable IDs, transcript health, Kusto validation, checkpoint creation, and rollback evidence converge before the first live rollback rehearsal."
        )
    elif warning_gate_labels:
        summary_status = "warn"
        summary_detail = f"{len(warning_gate_labels)} operator gate(s) still need follow-up: {', '.join(warning_gate_labels)}."
    else:
        summary_status = "ok"
        summary_detail = f"No blocking PM/operator gates remain for {edition_name}."

    summary_check = DoctorCheck(
        "Operator Gates",
        summary_status,
        summary_detail,
        metadata={
            "blocking_gate_labels": blocking_gate_labels,
            "warning_gate_labels": warning_gate_labels,
            "program_id": resolved.program.id,
            "edition": edition_name,
        },
    )
    return DoctorReport(
        edition=edition_name,
        checks=(summary_check, *gate_checks),
    )
