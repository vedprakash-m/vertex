from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.commands.doctor_checks.models import DoctorCheck, DoctorReport
from src.commands.metric import _build_metric_rollout_status, _build_program_metric_binding_probe
from src.core.edition_resolver import resolve_edition
from src.core.exceptions import ConfigError, QueryError
from src.core.kusto_query_loader import load_kpi_queries
from src.core.metric_binding_validator import MetricBindingProbe, compute_metric_binding_validation_hash, validate_metric_source_binding
from src.core.metric_models import MetricDefinition
from src.core.metric_registry import METRICS_ROOT, load_metric_definition_map
from src.core.reality_store import RealityStore


def run_metric_binding_doctor(
    *,
    edition_name: str,
    editions_root: Path,
    programs_root: Path,
    reality_db_root: Path | None,
    metric_binding_probe: MetricBindingProbe | None,
    metric_definitions: dict[str, MetricDefinition] | None,
    now: datetime | None,
    revalidation_days: int,
) -> DoctorReport:
    resolved = resolve_edition(edition_name, editions_root=editions_root, programs_root=programs_root)
    if resolved is None:
        return DoctorReport(
            edition=edition_name,
            checks=(DoctorCheck("Metric Bindings", "fail", f"Edition '{edition_name}' could not be resolved."),),
        )

    current_now = now or datetime.now(timezone.utc)
    store = RealityStore(resolved.program.id, db_root=reality_db_root)
    store.initialize()
    bindings = store.list_active_metric_source_bindings()
    definition_map = metric_definitions or load_metric_definition_map(metrics_root=METRICS_ROOT, as_of=current_now)
    rollout_statuses = tuple(
        _build_metric_rollout_status(store, query, definition_map)
        for query in load_kpi_queries(resolved.program.id, programs_root=programs_root)
    )
    eligible_rollout_statuses = tuple(status for status in rollout_statuses if status.eligible)
    missing_rollout_statuses = tuple(status for status in eligible_rollout_statuses if not status.ready)
    if not bindings:
        return DoctorReport(
            edition=edition_name,
            checks=(
                DoctorCheck(
                    "Metric Bindings",
                    "ok",
                    f"No active metric bindings found in the L1 store for program '{resolved.program.id}'.",
                    metadata={"active_binding_count": 0},
                ),
                build_metric_rollout_doctor_check(eligible_rollout_statuses, missing_rollout_statuses),
            ),
        )

    revalidation_due_before = current_now - timedelta(days=revalidation_days)
    stale_bindings = tuple(
        binding
        for binding in bindings
        if binding.validated and (binding.last_validated_at is None or binding.last_validated_at < revalidation_due_before)
    )
    probe = metric_binding_probe or _build_program_metric_binding_probe(resolved.program.id, programs_root=programs_root)

    revalidated_binding_ids: list[str] = []
    revalidation_failures: list[dict[str, str]] = []
    final_bindings: dict[str, Any] = {binding.binding_id: binding for binding in bindings}
    for binding in stale_bindings:
        try:
            refreshed = validate_metric_source_binding(
                binding,
                metric_definitions=definition_map,
                probe=probe,
                validated_at=current_now,
            )
        except (ConfigError, QueryError, ValueError) as error:
            failed_binding = binding if not binding.validated else replace(binding, validated=False)
            store.upsert_metric_source_binding(failed_binding)
            final_bindings[binding.binding_id] = failed_binding
            revalidation_failures.append({"binding_id": binding.binding_id, "error": str(error)})
            continue

        store.upsert_metric_source_binding(refreshed)
        final_bindings[binding.binding_id] = refreshed
        revalidated_binding_ids.append(binding.binding_id)

    final_binding_entries = tuple(final_bindings.values())
    unvalidated_binding_ids = [binding.binding_id for binding in final_binding_entries if not binding.validated]
    drifted_binding_ids = [
        binding.binding_id
        for binding in final_binding_entries
        if binding.validated
        and binding.last_validated_kql_hash is not None
        and binding.kql_template is not None
        and binding.last_validated_kql_hash != compute_metric_binding_validation_hash(binding)
    ]

    summary_parts = [f"{len(final_binding_entries)} active binding(s)"]
    if unvalidated_binding_ids:
        summary_parts.append(f"{len(unvalidated_binding_ids)} unvalidated")
    if revalidated_binding_ids:
        summary_parts.append(f"{len(revalidated_binding_ids)} revalidated")
    if revalidation_failures:
        summary_parts.append(f"{len(revalidation_failures)} revalidation failure(s)")
    if drifted_binding_ids:
        summary_parts.append(f"{len(drifted_binding_ids)} drifted since validation")
    summary_detail = "; ".join(summary_parts) + "."
    if unvalidated_binding_ids:
        summary_detail += " Run `vertex admin metric validate --program <program> --all` to validate newly added or failed bindings."
    summary_status = "warn" if (unvalidated_binding_ids or revalidation_failures or drifted_binding_ids) else "ok"

    checks = [
        DoctorCheck(
            "Metric Bindings",
            summary_status,
            summary_detail,
            metadata={
                "active_binding_count": len(final_binding_entries),
                "unvalidated_binding_ids": unvalidated_binding_ids,
                "revalidated_binding_ids": revalidated_binding_ids,
                "drifted_binding_ids": drifted_binding_ids,
                "revalidation_failures": revalidation_failures,
                "revalidation_due_before": revalidation_due_before.isoformat(),
            },
        ),
        build_metric_rollout_doctor_check(eligible_rollout_statuses, missing_rollout_statuses),
    ]

    if stale_bindings:
        if revalidation_failures:
            failure_detail = "; ".join(
                f"{entry['binding_id']}: {entry['error']}" for entry in revalidation_failures[:2]
            )
            if len(revalidation_failures) > 2:
                failure_detail = f"{failure_detail}; +{len(revalidation_failures) - 2} more"
            checks.append(
                DoctorCheck(
                    "Binding Revalidation",
                    "warn",
                    f"Revalidated {len(revalidated_binding_ids)} binding(s) older than {revalidation_days} days; new failures: {failure_detail}",
                    metadata={
                        "candidate_binding_ids": [binding.binding_id for binding in stale_bindings],
                        "revalidated_binding_ids": revalidated_binding_ids,
                        "revalidation_failures": revalidation_failures,
                    },
                )
            )
        else:
            checks.append(
                DoctorCheck(
                    "Binding Revalidation",
                    "ok",
                    f"Revalidated {len(revalidated_binding_ids)} binding(s) older than {revalidation_days} days.",
                    metadata={
                        "candidate_binding_ids": [binding.binding_id for binding in stale_bindings],
                        "revalidated_binding_ids": revalidated_binding_ids,
                    },
                )
            )
    else:
        checks.append(
            DoctorCheck(
                "Binding Revalidation",
                "ok",
                f"No bindings are due for revalidation inside the {revalidation_days}-day cadence window.",
                metadata={"candidate_binding_ids": []},
            )
        )

    if drifted_binding_ids:
        checks.append(
            DoctorCheck(
                "Binding Drift",
                "warn",
                f"Stored validation hash no longer matches the current validation query for {', '.join(drifted_binding_ids)}.",
                metadata={"drifted_binding_ids": drifted_binding_ids},
            )
        )
    else:
        checks.append(
            DoctorCheck(
                "Binding Drift",
                "ok",
                "No validation-query drift detected across active validated bindings.",
                metadata={"drifted_binding_ids": []},
            )
        )

    return DoctorReport(edition=edition_name, checks=tuple(checks))


def build_metric_rollout_doctor_check(
    eligible_rollout_statuses: tuple[Any, ...],
    missing_rollout_statuses: tuple[Any, ...],
) -> DoctorCheck:
    if not eligible_rollout_statuses:
        return DoctorCheck(
            "Metric Rollout",
            "ok",
            "No KPI catalog entries are currently eligible for deterministic assertion/binding rollout.",
            metadata={"eligible_query_ids": [], "missing_query_ids": []},
        )
    if not missing_rollout_statuses:
        return DoctorCheck(
            "Metric Rollout",
            "ok",
            f"All {len(eligible_rollout_statuses)} eligible KPI query rollout(s) already have active bindings and assertions in the reality store.",
            metadata={
                "eligible_query_ids": [status.query_id for status in eligible_rollout_statuses],
                "missing_query_ids": [],
            },
        )
    missing_ids = [status.query_id for status in missing_rollout_statuses]
    detail = ", ".join(missing_ids[:3])
    if len(missing_ids) > 3:
        detail = f"{detail}, +{len(missing_ids) - 3} more"
    return DoctorCheck(
        "Metric Rollout",
        "warn",
        f"{len(missing_rollout_statuses)} eligible KPI rollout(s) still missing active binding and/or assertion records: {detail}.",
        metadata={
            "eligible_query_ids": [status.query_id for status in eligible_rollout_statuses],
            "missing_query_ids": missing_ids,
        },
    )
