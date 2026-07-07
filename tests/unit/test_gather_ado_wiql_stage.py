"""Direct coverage for the extracted ADO WIQL gather stage."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, cast

from src.commands.gather_pipeline import ado_wiql_stage
from src.core.models_v2 import ADOConfig, KustoQuery, Program, Workstream


def _wiql_query(*, wiql: str, query_id: str = "query-a") -> KustoQuery:
    return KustoQuery(
        id=query_id,
        cluster="",
        database="",
        kql="",
        section="Section A",
        render_as="table",
        confidence="high",
        engine="wiql",
        wiql=wiql,
        workstream_ids=("acme",),
    )


def test_load_wiql_golden_query_signals_emits_wiql_and_graph_signals() -> None:
    program = Program(
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
    )
    workstreams = (Workstream(id="acme", name="Acme", area_paths=("One\\Adventure\\Acme",)),)
    executed_wiql: list[str] = []

    class _FakeADOClient:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        def list_team_iterations(self, timeframe: str | None = None, team: str | None = None) -> list[dict[str, object]]:
            assert timeframe == "current"
            assert team is None
            return [{"id": "iteration-24", "path": "One\\Sprint 24"}]

        def execute_wiql(self, wiql: str, top: int | None = None) -> list[int]:
            del top
            executed_wiql.append(wiql)
            return [1001, 1002]

    signals, ado_calls = ado_wiql_stage.load_wiql_golden_query_signals(
        program,
        workstreams,
        datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        queries=(
            _wiql_query(
                wiql="SELECT [System.Id] FROM WorkItems WHERE [System.IterationPath] = '{current_iteration_path}'",
                query_id="stg-current-iteration",
            ),
        ),
        ado_client_factory=cast(Any, _FakeADOClient),
        normalize_ado_team_name_fn=lambda value: None if value is None else value.strip() or None,
        expand_with_linked_items_fn=lambda client, seed_ids: {1003} if seed_ids == frozenset({1001, 1002}) else set(),
    )

    assert ado_calls == 2
    assert executed_wiql == ["SELECT [System.Id] FROM WorkItems WHERE [System.IterationPath] = 'One\\Sprint 24'"]
    assert [signal.source for signal in signals] == ["ado/wiql", "ado/graph"]
    assert signals[0].entity_refs == ("WI:1001", "WI:1002", "WS:acme")
    assert signals[1].entity_refs == ("WI:1003",)


def test_record_ado_wiql_query_state_preserves_last_success_on_error() -> None:
    query_state_sink: dict[str, dict[str, object]] = {}

    ado_wiql_stage.record_ado_wiql_query_state(
        query_state_sink,
        _wiql_query(wiql="SELECT [System.Id] FROM WorkItems", query_id="query-a"),
        work_item_count=0,
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        duration_ms=250,
        error="boom",
        previous_state={"last_succeeded_at": "2026-05-09T08:00:00+00:00", "value_last_4": [2.0, 2.0, 2.0]},
    )

    state = query_state_sink["query-a"]
    assert state["last_cycle_succeeded"] is False
    assert state["last_error"] == "boom"
    assert state["last_succeeded_at"] == datetime(2026, 5, 9, 8, 0, tzinfo=timezone.utc)
    assert state["value_last_4"] == [2.0, 2.0, 2.0]
