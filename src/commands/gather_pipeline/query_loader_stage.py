from __future__ import annotations

from dataclasses import replace
from typing import Callable

from src.core.knowledge_store import KnowledgeStore
from src.core.kusto_templates import KustoTemplateContext, render_kusto_query
from src.core.models_v2 import KustoQuery, Program, Workstream

IcmQueryPredicate = Callable[[KustoQuery], bool]


def load_kusto_queries(
    program_id: str,
    *,
    program: Program,
    knowledge: KnowledgeStore,
    workstreams: tuple[Workstream, ...] = (),
    apply_signal_source_scope: bool = False,
    filter_for_gather: bool = False,
    include_unvalidated: bool = False,
    is_icm_query_fn: IcmQueryPredicate,
) -> tuple[KustoQuery, ...]:
    direct_queries = tuple(
        query
        for query in (program.kusto.queries if program.kusto is not None else ())
        if query.engine == "kusto" and query_applies_to_program(query, program_id)
    )
    activated_golden_query_ids = {query_id for query_id in program.golden_queries}
    knowledge_queries = tuple(
        query
        for query in knowledge.golden_queries
        if query.engine == "kusto" and (query_applies_to_program(query, program_id) or query.id in activated_golden_query_ids)
    )

    merged: dict[str, KustoQuery] = {query.id: query for query in knowledge_queries}
    for query in direct_queries:
        merged[query.id] = query
    rendered = _render_queries(program=program, queries=tuple(merged.values()))
    if apply_signal_source_scope:
        rendered = scope_kusto_queries_to_workstreams(
            rendered,
            workstreams=workstreams,
            is_icm_query_fn=is_icm_query_fn,
        )
    if not filter_for_gather:
        return rendered
    return tuple(query for query in rendered if include_unvalidated or query.validated)


def load_ado_wiql_queries(
    program_id: str,
    *,
    program: Program,
    knowledge: KnowledgeStore,
    filter_for_gather: bool = False,
    include_unvalidated: bool = False,
) -> tuple[KustoQuery, ...]:
    activated_golden_query_ids = {query_id for query_id in program.golden_queries}
    queries = tuple(
        query
        for query in knowledge.golden_queries
        if query.engine == "wiql" and (query_applies_to_program(query, program_id) or query.id in activated_golden_query_ids)
    )
    rendered = _render_queries(program=program, queries=queries)
    if not filter_for_gather:
        return rendered
    return tuple(query for query in rendered if include_unvalidated or query.validated)


def scope_kusto_queries_to_workstreams(
    queries: tuple[KustoQuery, ...],
    *,
    workstreams: tuple[Workstream, ...],
    is_icm_query_fn: IcmQueryPredicate,
) -> tuple[KustoQuery, ...]:
    scoped_workstreams_by_query = kusto_workstreams_by_query_id(workstreams)
    if not scoped_workstreams_by_query:
        return queries

    scoped_queries: list[KustoQuery] = []
    for query in queries:
        if is_icm_query_fn(query):
            scoped_queries.append(query)
            continue
        scoped_workstream_ids = scoped_workstreams_by_query.get(query.id)
        if scoped_workstream_ids is None:
            continue
        merged_workstream_ids = tuple(dict.fromkeys((*query.workstream_ids, *scoped_workstream_ids)))
        scoped_queries.append(replace(query, workstream_ids=merged_workstream_ids))
    return tuple(scoped_queries)


def kusto_workstreams_by_query_id(workstreams: tuple[Workstream, ...]) -> dict[str, tuple[str, ...]]:
    workstream_ids_by_query: dict[str, list[str]] = {}
    for workstream in workstreams:
        signal_sources = workstream.signal_sources
        if signal_sources is None:
            continue
        for query_id in signal_sources.kusto_query_ids:
            if not query_id:
                continue
            workstream_ids_by_query.setdefault(query_id, []).append(workstream.id)
    return {
        query_id: tuple(dict.fromkeys(workstream_ids))
        for query_id, workstream_ids in workstream_ids_by_query.items()
    }


def query_applies_to_program(query: KustoQuery, program_id: str) -> bool:
    return not query.program_ids or program_id in query.program_ids


def _render_queries(*, program: Program, queries: tuple[KustoQuery, ...]) -> tuple[KustoQuery, ...]:
    ado_cfg = program.ado
    template_context = KustoTemplateContext(
        program_id=program.id,
        area_paths=ado_cfg.area_paths if ado_cfg is not None else (),
        date_window_days=ado_cfg.date_window_days if ado_cfg is not None else 30,
    )
    return tuple(render_kusto_query(query, context=template_context) for query in queries)
