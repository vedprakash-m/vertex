from __future__ import annotations

from pathlib import Path
import re
from typing import Any

import yaml

from src.commands.doctor_checks.models import DoctorCheck, DoctorReport
from src.core.edition_resolver import resolve_edition
from src.core.kusto_query_loader import load_kpi_queries


def run_charts_doctor(*, edition_name: str, editions_root: Path, programs_root: Path) -> DoctorReport:
    """
    Validate chart cache TTL vs edition cadence, attachment targets,
    exec-summary uniqueness, and renderer ID namespaces.

    Per spec §14: doctor must validate:
    - chart_cache_ttl_hours >= edition cadence hours (hard fail)
    - chart_cache_ttl_hours < cadence * 1.5 (advisory warn)
    - attachment targets reference known workstream section_ids
    - at most one exec-summary chart per effective edition
    - renderer IDs use :: namespace convention
    - schema validation for chart_config
    """
    resolved = resolve_edition(
        edition_name,
        editions_root=editions_root,
        programs_root=programs_root,
    )
    if resolved is None:
        return DoctorReport(edition=edition_name, checks=(DoctorCheck("Charts", "fail", f"Edition '{edition_name}' could not be resolved."),))

    program_id = resolved.program.id
    cadence_str = resolved.edition.cadence
    cadence_hours = cadence_hours_for_string(cadence_str)
    if cadence_hours is None:
        return DoctorReport(
            edition=edition_name,
            checks=(DoctorCheck("Charts", "fail", f"Unknown edition cadence '{cadence_str}'."),),
        )

    charts_enabled = resolved.raw_program.get("charts") is not None
    if not charts_enabled:
        return DoctorReport(
            edition=edition_name,
            checks=(DoctorCheck("Charts", "ok", "Chart pipeline is disabled for this program."),),
        )

    checks: list[DoctorCheck] = []

    try:
        queries = load_kpi_queries(program_id, programs_root=programs_root)
    except (OSError, yaml.YAMLError, KeyError, ValueError, AttributeError) as exc:
        return DoctorReport(
            edition=edition_name,
            checks=(DoctorCheck("Charts", "fail", f"Failed to load KPI queries: {exc}"),),
        )

    chart_queries = [query for query in queries if getattr(query, "chart_config", None) is not None]

    for query in chart_queries:
        ttl = getattr(query, "chart_cache_ttl_hours", 26)
        if ttl < cadence_hours:
            checks.append(
                DoctorCheck(
                    "Chart Cache TTL",
                    "fail",
                    f"Query '{query.id}' TTL ({ttl}h) is less than edition cadence ({cadence_hours}h). "
                    "Chart may expire before next issue is confirmed.",
                    metadata={"query_id": query.id, "ttl_hours": ttl, "cadence_hours": cadence_hours},
                )
            )
        elif ttl < cadence_hours * 1.5:
            checks.append(
                DoctorCheck(
                    "Chart Cache TTL",
                    "warn",
                    f"Query '{query.id}' TTL ({ttl}h) is less than 1.5× edition cadence ({cadence_hours}h). "
                    "Consider increasing TTL to reduce refresh pressure.",
                    metadata={
                        "query_id": query.id,
                        "ttl_hours": ttl,
                        "cadence_hours": cadence_hours,
                        "threshold_hours": cadence_hours * 1.5,
                    },
                )
            )

    known_workstream_ids = {workstream.id for workstream in resolved.workstreams}
    exec_summary_count = 0
    seen_renderer_ids: set[str] = set()

    for query in chart_queries:
        attachment = getattr(query, "attachment", None)
        if attachment is None:
            continue
        target = getattr(attachment, "target", None)
        if target is None:
            continue

        if target == "exec_summary":
            exec_summary_count += 1
        elif target.startswith("workstream:"):
            ws_target = target[len("workstream:") :]
            if ws_target not in known_workstream_ids:
                checks.append(
                    DoctorCheck(
                        "Chart Attachment Target",
                        "fail",
                        f"Query '{query.id}' attachment target 'workstream:{ws_target}' "
                        f"does not match any known workstream section_id. "
                        f"Known IDs: {', '.join(sorted(known_workstream_ids)) or '(none)'}",
                        metadata={"query_id": query.id, "target": target, "known_workstream_ids": sorted(known_workstream_ids)},
                    )
                )
        else:
            checks.append(
                DoctorCheck(
                    "Chart Attachment Target",
                    "fail",
                    f"Query '{query.id}' has invalid attachment target '{target}'. "
                    "Must be 'exec_summary' or 'workstream:<section_id>'.",
                    metadata={"query_id": query.id, "target": target},
                )
            )

    if exec_summary_count > 1:
        checks.append(
            DoctorCheck(
                "Chart Exec-Summary Uniqueness",
                "fail",
                f"Found {exec_summary_count} charts targeting 'exec_summary'. "
                "At most one exec-summary chart is allowed per effective edition.",
                metadata={"exec_summary_chart_count": exec_summary_count},
            )
        )

    schema_support = load_chart_schema_support()
    for query in chart_queries:
        chart_config = getattr(query, "chart_config", None)
        if not chart_config:
            continue
        if schema_support is not None:
            jsonschema, chart_config_schema, forbidden_chart_config_keys = schema_support
            forbidden = forbidden_chart_config_keys & set(chart_config.keys())
            if forbidden:
                checks.append(
                    DoctorCheck(
                        "Chart Config Schema",
                        "fail",
                        f"Query '{query.id}' chart_config uses reserved/deferred keys: {', '.join(sorted(forbidden))}. "
                        "These features are not yet implemented. Remove them from chart_config.",
                        metadata={"query_id": query.id, "forbidden_keys": sorted(forbidden)},
                    )
                )
            try:
                jsonschema.validate(chart_config, chart_config_schema)
            except jsonschema.ValidationError as error:
                checks.append(
                    DoctorCheck(
                        "Chart Config Schema",
                        "fail",
                        f"Query '{query.id}' chart_config is invalid: {error.message}. "
                        "Run `vertex doctor --charts` after correcting kpis.yaml.",
                        metadata={"query_id": query.id, "schema_error": error.message},
                    )
                )

    for query in chart_queries:
        renderer_id = getattr(query, "chart_renderer_id", None)
        if renderer_id is None:
            continue
        if "::" not in renderer_id:
            checks.append(
                DoctorCheck(
                    "Chart Renderer ID Namespace",
                    "fail",
                    f"Query '{query.id}' renderer_id '{renderer_id}' must use '::' namespace separator. "
                    "Use '<program>::<renderer_name>' format (e.g., 'acme::deploy_velocity').",
                    metadata={"query_id": query.id, "renderer_id": renderer_id},
                )
            )
        if renderer_id in seen_renderer_ids:
            checks.append(
                DoctorCheck(
                    "Chart Renderer ID Uniqueness",
                    "warn",
                    f"Duplicate renderer_id '{renderer_id}' across queries.",
                    metadata={"renderer_id": renderer_id},
                )
            )
        seen_renderer_ids.add(renderer_id)

    if not checks:
        return DoctorReport(
            edition=edition_name,
            checks=(
                DoctorCheck(
                    "Charts",
                    "ok",
                    f"Chart pipeline validated: {len(chart_queries)} chart query(ies), TTL >= {cadence_hours}h cadence, all attachment targets valid.",
                ),
            ),
        )

    return DoctorReport(edition=edition_name, checks=tuple(checks))


def cadence_hours_for_string(cadence: str) -> int | None:
    """Convert cadence string to approximate hours."""
    cadence_lower = cadence.lower().strip()
    if cadence_lower == "daily":
        return 24
    if cadence_lower == "weekly":
        return 168
    if cadence_lower == "biweekly":
        return 336
    if cadence_lower == "monthly":
        return 720
    match = re.search(r"every\s+(\d+)\s+days?", cadence_lower)
    if match:
        return int(match.group(1)) * 24
    return None


def load_chart_schema_support() -> tuple[Any, Any, frozenset[str]] | None:
    try:
        import jsonschema
        from src.core.charts.chart_config_schema import CHART_CONFIG_SCHEMA, FORBIDDEN_CHART_CONFIG_KEYS
    except ImportError:
        return None
    return jsonschema, CHART_CONFIG_SCHEMA, FORBIDDEN_CHART_CONFIG_KEYS
