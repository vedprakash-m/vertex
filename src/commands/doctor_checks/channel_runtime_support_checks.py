from __future__ import annotations

from pathlib import Path
from typing import Any

from src.commands.doctor_checks.kusto_checks import kusto_target_labels, load_doctor_kusto_queries
from src.core.kusto_templates import KustoTemplateContext


def current_doctor_kusto_targets(
    *,
    program_id: str,
    program: Any,
    programs_root: Path,
) -> tuple[str, ...]:
    ado_cfg = program.ado
    template_context = KustoTemplateContext(
        program_id=program.id,
        area_paths=ado_cfg.area_paths if ado_cfg is not None else (),
        date_window_days=ado_cfg.date_window_days if ado_cfg is not None else 30,
    )
    direct_queries = tuple(
        query
        for query in getattr(getattr(program, "kusto", None), "queries", ())
        if query.engine == "kusto" and (not query.program_ids or program_id in query.program_ids)
    )
    rendered_queries = load_doctor_kusto_queries(
        program_id,
        template_context=template_context,
        direct_queries=direct_queries,
        programs_root=programs_root,
    )
    return kusto_target_labels(rendered_queries)


def channel_auth_failure_detail(channel_name: str, last_error: str | None) -> str | None:
    if last_error is None:
        return None
    normalized = last_error.lower()
    if channel_name == "ado" and any(token in normalized for token in ("401", "unauthorized", "forbidden", "pat", "personal access token")):
        return "ADO PAT expired or revoked."
    if channel_name == "kusto" and any(token in normalized for token in ("aadsts", "azure cli", "az login", "defaultazurecredential", "credential", "token", "unauthorized", "forbidden", "authentication")):
        return "Azure CLI session expired or Kusto access failed; run 'az login' and verify cluster access."
    if channel_name in {"workiq", "transcript"} and any(token in normalized for token in ("agency", "workiq", "mcp", "timed out", "timeout", "not responding", "unavailable", "not found")):
        return "Agency CLI not responding or WorkIQ access failed; verify 'agency mcp list'."
    if channel_name == "icm" and any(token in normalized for token in ("401", "unauthorized", "forbidden", "client secret", "token", "credential")):
        return "IcM credentials expired or access failed; verify the configured IcM auth settings."
    return None
