from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.core.integration_types import ChannelRegistration, HydrationMode, RegistrationStatus
from src.core.kusto_hydration import KustoHydrationConfig, KustoHydrationProvider
from src.core.models_v2 import KustoQuery


def test_kusto_hydration_executes_registered_queries_and_normalizes_rows(tmp_path: Path) -> None:
    query = KustoQuery(
        id="query-a",
        cluster="https://cluster",
        database="db",
        kql="StormEvents | take 1",
        section="A",
        render_as="table",
        confidence="high",
        workstream_ids=("ws-a",),
        validated=True,
    )
    provider = KustoHydrationProvider(
        executor=lambda rendered_query: [{"Value": 1, "Name": "Row"}],
        query_loader=lambda program_id, programs_root: (query,),
    )
    registration = ChannelRegistration(
        channel="kusto",
        program_id="demo",
        provider_instance_id="default",
        ref_id="query-a",
        ref_kind="kusto_query",
        status=RegistrationStatus.ACTIVE,
        first_discovered_at=datetime(2026, 5, 24, tzinfo=timezone.utc),
        last_seen_at=datetime(2026, 5, 24, tzinfo=timezone.utc),
        workstream_ids=("ws-a",),
    )

    result = provider.hydrate(
        (registration,),
        datetime(2026, 5, 24, tzinfo=timezone.utc),
        "demo",
        KustoHydrationConfig(programs_root=tmp_path),
        mode=HydrationMode.FULL,
    )

    assert result.api_call_count == 1
    assert result.hydrated_ref_ids == (("query-a", "kusto_query"),)
    assert result.resources.result_sets[0].query_id == "query-a"
    assert result.resources.result_sets[0].rows[0] == {"Value": 1, "Name": "Row"}


def test_kusto_hydration_tracks_missing_queries_as_failures(tmp_path: Path) -> None:
    provider = KustoHydrationProvider(executor=lambda rendered_query: [], query_loader=lambda program_id, programs_root: ())
    registration = ChannelRegistration(
        channel="kusto",
        program_id="demo",
        provider_instance_id="default",
        ref_id="missing",
        ref_kind="kusto_query",
        status=RegistrationStatus.ACTIVE,
        first_discovered_at=datetime(2026, 5, 24, tzinfo=timezone.utc),
        last_seen_at=datetime(2026, 5, 24, tzinfo=timezone.utc),
    )

    result = provider.hydrate(
        (registration,),
        datetime(2026, 5, 24, tzinfo=timezone.utc),
        "demo",
        KustoHydrationConfig(programs_root=tmp_path),
    )

    assert result.failed_ref_ids == (("missing", "kusto_query"),)
    assert result.errors[0].ref_id == "missing"