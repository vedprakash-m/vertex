"""Direct coverage for the extracted query loader stage."""

from __future__ import annotations

from src.commands.gather_pipeline import query_loader_stage
from src.core.knowledge_store import KnowledgeStore
from src.core.models_v2 import (
    ADOConfig,
    KustoConfig,
    KustoQuery,
    Program,
    Workstream,
    WorkstreamSignalSources,
)


def _program(*, golden_query_ids: tuple[str, ...] = (), direct_queries: tuple[KustoQuery, ...] = ()) -> Program:
    return Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        kusto=KustoConfig(enabled=True, queries=direct_queries),
        golden_queries=golden_query_ids,
    )


def _knowledge(*queries: KustoQuery) -> KnowledgeStore:
    return KnowledgeStore(
        people_directory=(),
        people_profiles=(),
        teams=(),
        products=(),
        golden_queries=queries,
    )


def _kusto_query(
    query_id: str,
    *,
    database: str = "xdataanalytics",
    cluster: str = "https://adventure.kusto.windows.net",
    program_ids: tuple[str, ...] = ("acme",),
    workstream_ids: tuple[str, ...] = (),
    validated: bool = True,
    engine: str = "kusto",
    wiql: str | None = None,
) -> KustoQuery:
    return KustoQuery(
        id=query_id,
        cluster=cluster,
        database=database,
        kql="Metrics | take 1",
        section=query_id,
        render_as="table",
        confidence="high",
        program_ids=program_ids,
        workstream_ids=workstream_ids,
        validated=validated,
        engine=engine,
        wiql=wiql,
    )


def _workstream(workstream_id: str, *query_ids: str) -> Workstream:
    return Workstream(
        id=workstream_id,
        name=workstream_id.upper(),
        signal_sources=WorkstreamSignalSources(kusto_query_ids=query_ids),
    )


def _is_icm_query(query: KustoQuery) -> bool:
    return query.database.strip().lower() == "icmdatawarehouse" or query.cluster.strip().lower().startswith("https://icmcluster.")


def test_load_kusto_queries_applies_signal_source_scope_but_keeps_icm_queries() -> None:
    queries = query_loader_stage.load_kusto_queries(
        "acme",
        program=_program(),
        knowledge=_knowledge(
            _kusto_query("velocity-p50"),
            _kusto_query("icm-active", database="IcMDataWarehouse", cluster="https://icmcluster.kusto.windows.net"),
        ),
        workstreams=(_workstream("acme", "velocity-p50"),),
        apply_signal_source_scope=True,
        is_icm_query_fn=_is_icm_query,
    )

    assert [query.id for query in queries] == ["velocity-p50", "icm-active"]
    assert queries[0].workstream_ids == ("acme",)
    assert queries[1].workstream_ids == ()


def test_load_kusto_queries_keeps_activated_golden_query_even_without_program_match() -> None:
    queries = query_loader_stage.load_kusto_queries(
        "acme",
        program=_program(golden_query_ids=("fleet-health",)),
        knowledge=_knowledge(_kusto_query("fleet-health", program_ids=("other-program",))),
        filter_for_gather=True,
        is_icm_query_fn=_is_icm_query,
    )

    assert [query.id for query in queries] == ["fleet-health"]


def test_load_ado_wiql_queries_filters_unvalidated_queries_for_gather() -> None:
    queries = query_loader_stage.load_ado_wiql_queries(
        "acme",
        program=_program(golden_query_ids=("wiql-active",)),
        knowledge=_knowledge(
            _kusto_query("wiql-active", engine="wiql", wiql="Select [System.Id] From WorkItems", validated=False),
            _kusto_query("wiql-ready", engine="wiql", wiql="Select [System.Id] From WorkItems", validated=True),
        ),
        filter_for_gather=True,
        include_unvalidated=False,
    )

    assert [query.id for query in queries] == ["wiql-ready"]


def test_golden_query_has_explicit_non_authoritative_classification() -> None:
    query = _kusto_query("armada-evidence")

    assert query.classification == "validation"
    assert query.is_authoritative_delivery_scope is False
