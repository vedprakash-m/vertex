from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from src.commands.doctor_checks.models import ADOProbeResult, DoctorCheck, DoctorReport
from src.core.exceptions import AuthError, QueryError
from src.core.kusto_templates import KustoTemplateContext
from src.core.models_v2 import KustoQuery
from src.m365.agency_bridge import AgencyCapabilities


def run_auth_doctor(
    *,
    edition_name: str,
    reports_root: Path,
    editions_root: Path,
    programs_root: Path,
    ado_probe: Callable[..., ADOProbeResult] | None,
    kusto_probe: Callable[[KustoQuery], None] | None,
    load_bundle_fn: Callable[..., Any],
    probe_ado_access_fn: Callable[[Any], ADOProbeResult],
    token_check_fn: Callable[[ADOProbeResult], DoctorCheck],
    mail_preview_check_fn: Callable[[], DoctorCheck],
    agency_bridge_factory: Callable[[], Any],
    agency_cli_check_fn: Callable[[AgencyCapabilities], DoctorCheck],
    resolve_edition_fn: Callable[..., Any],
    load_doctor_kusto_queries_fn: Callable[..., tuple[KustoQuery, ...]],
    kusto_validation_check_fn: Callable[[tuple[KustoQuery, ...]], DoctorCheck | None],
    icm_kusto_check_fn: Callable[[tuple[KustoQuery, ...]], DoctorCheck | None],
    capability_review_check_fn: Callable[..., DoctorCheck | None],
    kusto_target_labels_fn: Callable[[tuple[KustoQuery, ...]], tuple[str, ...]],
    validate_kusto_query_definitions_fn: Callable[[tuple[KustoQuery, ...]], tuple[str, ...]],
    summarize_kusto_targets_fn: Callable[[tuple[str, ...]], str],
    build_live_kusto_query_probe_fn: Callable[..., Callable[[tuple[KustoQuery, ...]], Any]],
) -> DoctorReport:
    bundle = load_bundle_fn(
        edition_name,
        reports_root=reports_root,
        editions_root=editions_root,
        programs_root=programs_root,
    )
    probe_result = (ado_probe or probe_ado_access_fn)(bundle)
    agency_caps = agency_bridge_factory().probe()
    checks: list[DoctorCheck] = [
        DoctorCheck(
            "ADO Access",
            "ok" if probe_result.reachable else "fail",
            probe_result.detail,
        ),
        token_check_fn(probe_result),
        mail_preview_check_fn(),
        agency_cli_check_fn(agency_caps),
    ]
    resolved = resolve_edition_fn(edition_name, editions_root=editions_root, programs_root=programs_root)
    workiq_check = workiq_auth_check(resolved=resolved, agency_caps=agency_caps)
    if workiq_check is not None:
        checks.append(workiq_check)
    kusto_check = kusto_auth_check(
        edition_name=edition_name,
        editions_root=editions_root,
        programs_root=programs_root,
        kusto_probe=kusto_probe,
        resolve_edition_fn=resolve_edition_fn,
        load_doctor_kusto_queries_fn=load_doctor_kusto_queries_fn,
        kusto_target_labels_fn=kusto_target_labels_fn,
        validate_kusto_query_definitions_fn=validate_kusto_query_definitions_fn,
        summarize_kusto_targets_fn=summarize_kusto_targets_fn,
        build_live_kusto_query_probe_fn=build_live_kusto_query_probe_fn,
    )
    if kusto_check is not None:
        checks.append(kusto_check)
    if resolved is not None and resolved.program.kusto is not None and resolved.program.kusto.enabled:
        ado = resolved.program.ado
        kusto_queries = load_doctor_kusto_queries_fn(
            resolved.paths.program_id,
            template_context=KustoTemplateContext(
                program_id=resolved.program.id,
                area_paths=ado.area_paths if ado is not None else (),
                date_window_days=ado.date_window_days if ado is not None else None,
            ),
            direct_queries=resolved.program.kusto.queries,
            programs_root=programs_root,
        )
        if kusto_queries:
            validation_check = kusto_validation_check_fn(kusto_queries)
            if validation_check is not None:
                checks.append(validation_check)
            icm_check = icm_kusto_check_fn(kusto_queries)
            if icm_check is not None:
                checks.append(icm_check)
    capability_review_check = capability_review_check_fn(
        resolved.program.id if resolved is not None else None,
        programs_root,
        warn_on_incomplete=True,
    )
    if capability_review_check is not None:
        checks.append(capability_review_check)
    return DoctorReport(edition=edition_name, checks=tuple(checks))


def workiq_auth_check(*, resolved: Any | None, agency_caps: AgencyCapabilities) -> DoctorCheck | None:
    if resolved is None:
        return None
    m365 = getattr(resolved.program, "m365", None)
    program_id = resolved.paths.program_id
    if m365 is None or not m365.enabled:
        return DoctorCheck(
            "WorkIQ Access",
            "warn",
            f"programs/{program_id}/program.yaml has m365.enabled=false; WorkIQ gather is disabled.",
            metadata={
                "m365_enabled": False,
                "query_count": 0,
                "prefer_agency": None if m365 is None else m365.prefer_agency,
            },
        )
    if not m365.workiq_queries:
        return DoctorCheck(
            "WorkIQ Access",
            "warn",
            f"programs/{program_id}/program.yaml has m365.enabled=true but no m365.workiq_queries configured; WorkIQ gather cannot build prompts.",
            metadata={
                "m365_enabled": True,
                "query_count": 0,
                "prefer_agency": m365.prefer_agency,
            },
        )

    query_count = len(m365.workiq_queries)
    query_label = f"{query_count} query target{'s' if query_count != 1 else ''}"
    metadata = {
        "m365_enabled": True,
        "query_count": query_count,
        "prefer_agency": m365.prefer_agency,
        "query_names": sorted(m365.workiq_queries),
    }
    if not agency_caps.available:
        return DoctorCheck(
            "WorkIQ Access",
            "warn",
            f"WorkIQ is configured with {query_label}, but Agency CLI is unavailable; install or enable WorkIQ MCP support before running `vertex gather --workiq`.",
            metadata=metadata,
        )
    if not agency_caps.has_workiq:
        return DoctorCheck(
            "WorkIQ Access",
            "warn",
            f"WorkIQ is configured with {query_label}, but Agency CLI is reachable without the WorkIQ MCP server.",
            metadata=metadata,
        )

    available_tools = tuple(agency_caps.server_tools.get("workiq", ()))
    if available_tools:
        missing_tools = missing_workiq_tools(available_tools)
        metadata["available_tools"] = available_tools
        metadata["missing_tools"] = missing_tools
        if missing_tools:
            return DoctorCheck(
                "WorkIQ Access",
                "warn",
                f"WorkIQ is configured with {query_label}, but the active WorkIQ MCP server is missing required tool(s): {', '.join(missing_tools)}.",
                metadata=metadata,
            )

    return DoctorCheck(
        "WorkIQ Access",
        "ok",
        f"WorkIQ is configured with {query_label}; Agency CLI WorkIQ support is available.",
        metadata=metadata,
    )


def missing_workiq_tools(available_tools: tuple[str, ...]) -> tuple[str, ...]:
    available = {tool.strip() for tool in available_tools if tool.strip()}
    if not available or "ask_work_iq" in available:
        return ()
    missing: list[str] = []
    for required in ("search_emails", "get_transcript"):
        if required not in available:
            missing.append(required)
    if "get_meetings" not in available:
        missing.append("get_meetings")
    return tuple(missing)


def kusto_auth_check(
    *,
    edition_name: str,
    editions_root: Path,
    programs_root: Path,
    kusto_probe: Callable[[KustoQuery], None] | None,
    resolve_edition_fn: Callable[..., Any],
    load_doctor_kusto_queries_fn: Callable[..., tuple[KustoQuery, ...]],
    kusto_target_labels_fn: Callable[[tuple[KustoQuery, ...]], tuple[str, ...]],
    validate_kusto_query_definitions_fn: Callable[[tuple[KustoQuery, ...]], tuple[str, ...]],
    summarize_kusto_targets_fn: Callable[[tuple[str, ...]], str],
    build_live_kusto_query_probe_fn: Callable[..., Callable[[tuple[KustoQuery, ...]], Any]],
) -> DoctorCheck | None:
    resolved = resolve_edition_fn(edition_name, editions_root=editions_root, programs_root=programs_root)
    if resolved is None:
        return DoctorCheck("Kusto Access", "fail", f"Edition '{edition_name}' could not be resolved.")
    if resolved.program.kusto is None or not resolved.program.kusto.enabled:
        return DoctorCheck(
            "Kusto Access",
            "warn",
            f"programs/{resolved.paths.program_id}/program.yaml has kusto.enabled=false; Kusto gather and auth probing are disabled.",
            metadata={
                "query_count": 0,
                "query_ids": [],
                "cluster_targets": [],
                "kusto_enabled": False,
            },
        )

    ado = resolved.program.ado
    queries = load_doctor_kusto_queries_fn(
        resolved.paths.program_id,
        template_context=KustoTemplateContext(
            program_id=resolved.program.id,
            area_paths=ado.area_paths if ado is not None else (),
            date_window_days=ado.date_window_days if ado is not None else None,
        ),
        direct_queries=resolved.program.kusto.queries,
        programs_root=programs_root,
    )
    probe_queries = tuple(query for query in queries if query.cluster.strip() and query.database.strip() and query.kql.strip())
    if not probe_queries:
        return DoctorCheck("Kusto Access", "warn", "Kusto is enabled but no applicable query targets were found for auth probing.")

    targets = kusto_target_labels_fn(probe_queries)
    metadata = {
        "query_count": len(probe_queries),
        "query_ids": [query.id for query in probe_queries],
        "cluster_targets": list(targets),
        "kusto_enabled": True,
        "skipped_query_ids": [
            query.id
            for query in queries
            if query.id not in {probe_query.id for probe_query in probe_queries}
        ],
    }
    problems = validate_kusto_query_definitions_fn(probe_queries)
    if problems:
        detail = "; ".join(problems[:2])
        if len(problems) > 2:
            detail = f"{detail}; +{len(problems) - 2} more"
        return DoctorCheck("Kusto Access", "fail", detail, metadata=metadata)
    try:
        if kusto_probe is not None:
            for query in probe_queries:
                kusto_probe(query)
        else:
            build_live_kusto_query_probe_fn(log_failures=False)(probe_queries)
    except (AuthError, QueryError) as error:
        return DoctorCheck("Kusto Access", "fail", str(error), metadata=metadata)

    return DoctorCheck(
        "Kusto Access",
        "ok",
        f"Kusto auth probe succeeded for {summarize_kusto_targets_fn(targets)}.",
        metadata=metadata,
    )
