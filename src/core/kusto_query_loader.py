from __future__ import annotations
from datetime import datetime
from pathlib import Path
from typing import Any

from src.core.exceptions import ConfigError, StateError
from src.core.gather_state_store import load_gather_query_states
from src.core.knowledge_store import PROGRAMS_ROOT, load_program_knowledge
from src.core.models_v2 import AttachmentConfig, KustoQuery
from src.core.yaml_utils import load_yaml_mapping


def load_kpi_queries(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[KustoQuery, ...]:
    knowledge = load_program_knowledge(program_id, programs_root=programs_root)
    golden_queries = tuple(
        query
        for query in knowledge.golden_queries
        if query.engine == "kusto" and _query_applies_to_program(query, program_id)
    )
    gather_query_state = _load_gather_query_state(program_id, programs_root=programs_root)

    kpis_path = programs_root / program_id / "kpis.yaml"
    if not kpis_path.exists():
        return tuple(_apply_gather_query_state(query, gather_query_state.get(query.id)) for query in golden_queries)

    document = load_yaml_mapping(kpis_path)
    kpi_queries = tuple(
        KustoQuery(
            id=_require_str(entry, "id", kpis_path),
            cluster=_optional_str(entry.get("cluster")) or "",
            database=_optional_str(entry.get("database")) or "",
            kql=_optional_str(entry.get("kql")) or (_load_kql_from_file(str(entry.get("kql_file")), programs_root.parent) if entry.get("kql_file") else ""),
            section=_optional_str(entry.get("section")) or _require_str(entry, "id", kpis_path),
            render_as=_optional_str(entry.get("render_as")) or "table",
            confidence=_optional_str(entry.get("confidence")) or "medium",
            reference_url=_optional_str(entry.get("reference_url")),
            caveats=_string_tuple(entry.get("caveats", [])),
            kusto_section_validates_slice=bool(entry.get("kusto_section_validates_slice", False)),
            program_ids=_string_tuple(entry.get("program_ids", [])),
            workstream_ids=_string_tuple(entry.get("workstream_ids", [])),
            validated=bool(entry.get("validated", False)),
            refresh_on_gather=bool(entry.get("refresh_on_gather", False)),
            label=_optional_str(entry.get("label")),
            result_column=_optional_str(entry.get("result_column")),
            catalog_source=_string_dict(entry.get("catalog_source")),
            owner_alias=_optional_str(entry.get("owner_alias")),
            expected_cardinality=_optional_str(entry.get("expected_cardinality")) or "zero_ok",
            kusto_no_safety=bool(entry.get("kusto_no_safety", False)),
            metric_id=_optional_str(entry.get("metric_id")),
            assertion_ids=_string_tuple(entry.get("assertion_ids", [])),
            engine=_optional_str(entry.get("engine")) or "kusto",
            wiql=_optional_str(entry.get("wiql")),
            # Chart pipeline fields (R3)
            chart_renderer_id=_optional_str(entry.get("chart_renderer_id")),
            chart_config=entry.get("chart_config"),
            attachment=_parse_attachment(entry.get("attachment"), kpis_path),
            chart_cache_ttl_hours=int(entry.get("chart_cache_ttl_hours", 26)),
            chart_blocks_publish=bool(entry.get("chart_blocks_publish", False)),
            fallback_on_empty_rows=bool(entry.get("fallback_on_empty_rows", False)),
            chapter=_optional_str(entry.get("chapter")),
            timeout_seconds=int(entry["timeout_seconds"]) if entry.get("timeout_seconds") is not None else None,
        )
        for entry in document.get("kpis", [])
        if isinstance(entry, dict)
    )

    merged: dict[str, KustoQuery] = {}
    for query in (*golden_queries, *kpi_queries):
        if query.id in merged:
            raise ConfigError(f"Duplicate Kusto query id '{query.id}' found while loading {kpis_path}")
        merged[query.id] = _apply_gather_query_state(query, gather_query_state.get(query.id))
    return tuple(merged.values())


def kpi_queries_for_workstream(
    queries: tuple[KustoQuery, ...],
    workstream_id: str,
    *,
    refresh_only: bool = False,
) -> tuple[KustoQuery, ...]:
    scoped = tuple(query for query in queries if workstream_id in query.workstream_ids)
    if not refresh_only:
        return scoped
    return tuple(query for query in scoped if query.refresh_on_gather)


def _query_applies_to_program(query: KustoQuery, program_id: str) -> bool:
    return not query.program_ids or program_id in query.program_ids


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in value if isinstance(item, (str, int, float)) and str(item).strip())


def _string_dict(value: Any) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    mapped = {
        str(key).strip(): str(item).strip()
        for key, item in value.items()
        if str(key).strip() and isinstance(item, (str, int, float)) and str(item).strip()
    }
    return mapped or None


def _optional_str(value: Any) -> str | None:
    if not isinstance(value, (str, int, float)):
        return None
    text = str(value).strip()
    return text or None


def _load_gather_query_state(program_id: str, *, programs_root: Path) -> dict[str, dict[str, Any]]:
    try:
        return load_gather_query_states(program_id, programs_root=programs_root)
    except StateError as error:
        raise ConfigError(f"Invalid gather state payload for program '{program_id}'") from error


def _apply_gather_query_state(query: KustoQuery, state: dict[str, Any] | None) -> KustoQuery:
    if state is None:
        return query
    validated_at = _parse_optional_datetime(state.get("validated_at"))
    if validated_at is None:
        validated_at = _parse_optional_datetime(state.get("last_succeeded_at"))
    return KustoQuery(
        id=query.id,
        cluster=query.cluster,
        database=query.database,
        kql=query.kql,
        section=query.section,
        render_as=query.render_as,
        confidence=query.confidence,
        reference_url=query.reference_url,
        caveats=query.caveats,
        kusto_section_validates_slice=query.kusto_section_validates_slice,
        program_ids=query.program_ids,
        workstream_ids=query.workstream_ids,
        validated=query.validated,
        refresh_on_gather=query.refresh_on_gather,
        label=query.label,
        result_column=query.result_column,
        catalog_source=query.catalog_source,
        validated_at=validated_at,
        owner_alias=query.owner_alias,
        expected_cardinality=query.expected_cardinality,
        kusto_no_safety=query.kusto_no_safety,
        last_cycle_succeeded=_parse_optional_bool(state.get("last_cycle_succeeded")),
        metric_id=query.metric_id,
        assertion_ids=query.assertion_ids,
        engine=query.engine,
        wiql=query.wiql,
        # Chart pipeline fields (R3)
        chart_renderer_id=query.chart_renderer_id,
        chart_config=query.chart_config,
        attachment=query.attachment,
        chart_cache_ttl_hours=query.chart_cache_ttl_hours,
        chart_blocks_publish=query.chart_blocks_publish,
        fallback_on_empty_rows=query.fallback_on_empty_rows,
        chapter=query.chapter,
        timeout_seconds=query.timeout_seconds,
    )


def _parse_optional_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_optional_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def _parse_attachment(value: Any, path: Path) -> AttachmentConfig | None:
    """Parse optional attachment dict into AttachmentConfig."""
    if not isinstance(value, dict):
        return None
    target = _optional_str(value.get("target"))
    if not target:
        return None
    position = _optional_str(value.get("position")) or "after"
    if position != "after":
        raise ConfigError(
            f"attachment.position must be 'after' in R3; got '{position}' in {path}"
        )
    fallback = _optional_str(value.get("fallback")) or "standalone"
    if fallback not in ("standalone", "suppress"):
        fallback = "standalone"
    return AttachmentConfig(target=target, position="after", fallback=fallback)  # type: ignore[arg-type]


def _require_str(entry: dict[str, Any], key: str, path: Path) -> str:
    value = _optional_str(entry.get(key))
    if value is None:
        raise ConfigError(f"Missing required string '{key}' in {path}")
    return value


def _load_kql_from_file(rel_path: str, repo_root: Path) -> str:
    path = repo_root / rel_path
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""