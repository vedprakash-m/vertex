from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.core.kusto_discovery import KustoDiscoveryConfig, KustoDiscoveryProvider
from src.core.integration_types import DiscoveryCompleteness
from src.core.models_v2 import KustoQuery


def test_kusto_discovery_catalogs_validated_queries_and_workstreams(tmp_path: Path) -> None:
    queries = (
        KustoQuery(
            id="query-a",
            cluster="https://cluster",
            database="db",
            kql="StormEvents | take 1",
            section="A",
            render_as="table",
            confidence="high",
            workstream_ids=("ws-a", "ws-b"),
            validated=True,
        ),
        KustoQuery(
            id="query-b",
            cluster="https://cluster",
            database="db",
            kql="StormEvents | take 1",
            section="B",
            render_as="table",
            confidence="medium",
            workstream_ids=(),
            validated=False,
        ),
    )
    provider = KustoDiscoveryProvider(query_loader=lambda program_id, programs_root: queries)

    result = provider.discover("demo", KustoDiscoveryConfig(programs_root=tmp_path), ())

    assert result.completeness is DiscoveryCompleteness.FULL
    assert [ref.registration.ref_id for ref in result.discovered_refs] == ["query-a"]
    assert {binding.workstream_id for binding in result.discovered_refs[0].bindings} == {"ws-a", "ws-b"}


def test_kusto_discovery_can_include_unvalidated_queries(tmp_path: Path) -> None:
    query = KustoQuery(
        id="query-b",
        cluster="https://cluster",
        database="db",
        kql="StormEvents | take 1",
        section="B",
        render_as="table",
        confidence="medium",
        workstream_ids=(),
        validated=False,
    )
    provider = KustoDiscoveryProvider(query_loader=lambda program_id, programs_root: (query,))

    result = provider.discover("demo", KustoDiscoveryConfig(programs_root=tmp_path, include_unvalidated=True), ())

    assert [ref.registration.ref_id for ref in result.discovered_refs] == ["query-b"]
    assert result.discovered_refs[0].bindings[0].workstream_id is None