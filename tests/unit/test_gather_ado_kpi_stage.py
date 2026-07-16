"""Direct coverage for the extracted ADO KPI gather stage."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, cast

from src.commands.gather_pipeline import ado_kpi_stage
from src.core.ado_client import ADO_WIQL_DEFAULT_TOP
from src.core.models import Confidence
from src.core.models_v2 import ADOConfig, KustoConfig, KustoQuery, Program, Signal, Workstream


def _program() -> Program:
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
        kusto=KustoConfig(enabled=True),
    )


def _wiql_query(*, render_as: str = "metric_highlight", query_id: str = "stg-open") -> KustoQuery:
    return KustoQuery(
        id=query_id,
        cluster="",
        database="",
        kql="",
        section="STG Validation",
        render_as=render_as,
        confidence="medium",
        engine="wiql",
        wiql="SELECT [System.Id] FROM WorkItems WHERE [System.IterationPath] = '{current_iteration_path}'",
        workstream_ids=("acme",),
        result_column="OpenValidationItems",
    )


def _ado_pr_query(query_id: str = "open-pr-age") -> KustoQuery:
    return KustoQuery(
        id=query_id,
        cluster="",
        database="",
        kql="",
        section="Deployment Velocity",
        render_as="metric_highlight",
        confidence="medium",
        engine="ado_pr",
        workstream_ids=("acme",),
        result_column="P90AgeDays",
    )


def _build_signal(
    *,
    query: KustoQuery,
    rows: list[dict[str, Any]],
    program_id: str,
    as_of: datetime,
    entity_refs: tuple[str, ...] | None = None,
) -> Signal | None:
    if not rows:
        return None
    return Signal(
        id=f"{program_id}:{query.id}",
        timestamp=as_of,
        source="kusto_kpi",
        program_id=program_id,
        workstream_id=None,
        entity_refs=entity_refs or (),
        text=f"KPI {query.id}",
        raw_ref=f"kusto_kpi:{query.id}",
        confidence=Confidence.MEDIUM,
        metadata={"query_id": query.id, "result_json": "{}"},
    )


def test_execute_wiql_kpi_query_builds_table_rows_in_wiql_order() -> None:
    executed_wiql: list[str] = []

    class _FakeADOClient:
        def list_team_iterations(self, timeframe: str | None = None, team: str | None = None) -> list[dict[str, object]]:
            assert timeframe == "current"
            assert team is None
            return [{"path": "One\\Sprint 24"}]

        def execute_wiql(self, wiql: str, top: int | None = None) -> list[int]:
            del top
            executed_wiql.append(wiql)
            return [2002, 2001]

        def query_work_items_batch(self, work_item_ids: list[int], fields: tuple[str, ...]) -> list[dict[str, object]]:
            assert work_item_ids == [2002, 2001]
            assert fields == (
                "System.Id",
                "System.Title",
                "System.State",
                "System.AreaPath",
                "System.IterationPath",
                "Microsoft.VSTS.Scheduling.TargetDate",
                "System.ChangedDate",
                "System.AssignedTo",
                "System.Tags",
            )
            return [
                {
                    "id": 2001,
                    "fields": {
                        "System.Id": 2001,
                        "System.Title": "Dock cluster A",
                        "System.State": "Active",
                    },
                },
                {
                    "id": 2002,
                    "fields": {
                        "System.Id": 2002,
                        "System.Title": "Dock cluster B",
                        "System.State": "Committed",
                    },
                },
            ]

    rows = ado_kpi_stage.execute_wiql_kpi_query(
        _wiql_query(render_as="table", query_id="buildout-pipeline"),
        program=_program(),
        workstreams=(Workstream(id="acme", name="Acme", area_paths=("One\\Adventure\\Acme",)),),
        client=cast(Any, _FakeADOClient()),
        as_of=datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc),
        current_iteration_path_by_team={},
        normalize_ado_team_name_fn=lambda value: None if value is None else value.strip() or None,
    )

    assert executed_wiql == ["SELECT [System.Id] FROM WorkItems WHERE [System.IterationPath] = 'One\\Sprint 24'"]
    assert [row["WorkItemId"] for row in rows] == [2002, 2001]
    assert rows[0]["Title"] == "Dock cluster B"


def test_execute_ado_pr_kpi_query_aggregates_pr_age_and_entity_refs() -> None:
    class _FakeADOClient:
        def list_pull_requests(self, repository_id: str, *, status: str = "active", top: int = 100) -> list[dict[str, object]]:
            assert status == "active"
            assert top == 100
            if repository_id == "repo-42":
                return [
                    {
                        "pullRequestId": 301,
                        "title": "Stabilize rollout for WI:12345",
                        "status": "active",
                        "creationDate": "2026-05-02T08:00:00Z",
                        "repository": {"id": repository_id, "name": "XStoreApp"},
                    },
                    {
                        "pullRequestId": 302,
                        "title": "Tune validation gates",
                        "status": "active",
                        "creationDate": "2026-05-08T08:00:00Z",
                        "repository": {"id": repository_id, "name": "XStoreApp"},
                    },
                ]
            return []

    rows = ado_kpi_stage.execute_ado_pr_kpi_query(
        _ado_pr_query(),
        workstreams=(
            Workstream(
                id="acme",
                name="Acme",
                area_paths=("One\\Adventure\\Acme",),
                ado_repository_ids=("repo-42",),
            ),
        ),
        client=cast(Any, _FakeADOClient()),
        as_of=datetime(2026, 5, 12, 8, 0, tzinfo=timezone.utc),
        summarize_pull_requests_fn=lambda **kwargs: {
            "metadata": {
                "repository_id": kwargs["repository_id"],
                "repository_name": "XStoreApp",
                "open_pr_count": 2,
            }
        },
        pull_request_provider_ref_fn=lambda pull_request, repository_name: (
            f"PR:{repository_name}/{pull_request['pullRequestId']}"
        ),
        pull_request_entity_refs_fn=lambda pull_request, repository_name: (
            f"PR:{repository_name}/{pull_request['pullRequestId']}",
            "WI:12345" if pull_request["pullRequestId"] == 301 else "WI:67890",
        ),
    )

    assert len(rows) == 1
    assert rows[0]["P90AgeDays"] == 10.0
    assert rows[0]["OpenPrCount"] == 2
    assert rows[0]["EntityRefs"] == ["PR:XStoreApp/301", "WI:12345", "PR:XStoreApp/302", "WI:67890"]


def test_build_kusto_kpi_signals_records_query_state_and_entity_refs() -> None:
    workstreams = (Workstream(id="acme", name="Acme", area_paths=("One\\Adventure\\Acme",)),)
    query_state_sink: dict[str, dict[str, Any]] = {}
    seen_entity_refs: list[tuple[str, ...] | None] = []

    class _FakeADOClient:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        def list_team_iterations(self, timeframe: str | None = None, team: str | None = None) -> list[dict[str, object]]:
            assert timeframe == "current"
            assert team is None
            return [{"path": "One\\Sprint 24"}]

        def execute_wiql(self, wiql: str, top: int | None = None) -> list[int]:
            del top
            assert wiql.endswith("'One\\Sprint 24'")
            return [1001, 1002]

    def _capture_signal(**kwargs: Any) -> Signal | None:
        seen_entity_refs.append(kwargs["entity_refs"])
        return _build_signal(**kwargs)

    signals = ado_kpi_stage.build_kusto_kpi_signals(
        queries=(_wiql_query(),),
        program=_program(),
        program_id="acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        workstreams=workstreams,
        executor=lambda query: [],
        ado_client_factory=cast(Any, _FakeADOClient),
        normalize_ado_team_name_fn=lambda value: None if value is None else value.strip() or None,
        record_kusto_query_state_fn=lambda *args, **kwargs: None,
        build_kusto_kpi_signal_fn=_capture_signal,
        summarize_pull_requests_fn=lambda **kwargs: None,
        pull_request_provider_ref_fn=lambda pull_request, repository_name: None,
        pull_request_entity_refs_fn=lambda pull_request, repository_name: (),
        query_state_sink=query_state_sink,
        previous_query_states={"stg-open": {"value_last_4": [1.0]}},
    )

    assert len(signals) == 1
    assert seen_entity_refs == [("WI:1001", "WI:1002")]
    assert query_state_sink["stg-open"]["row_count"] == 2
    assert query_state_sink["stg-open"]["value_last_4"] == [1.0, 2.0]


def test_build_kusto_kpi_signals_records_kusto_errors_via_callback() -> None:
    recorded: list[dict[str, Any]] = []

    def _record_kusto_state(
        query_state_sink: dict[str, dict[str, Any]] | None,
        query: KustoQuery,
        *,
        rows: list[dict[str, Any]],
        as_of: datetime,
        duration_ms: int,
        error: str | None = None,
        previous_state: dict[str, Any] | None = None,
    ) -> None:
        del query_state_sink, as_of, duration_ms
        recorded.append(
            {
                "query_id": query.id,
                "rows": rows,
                "error": error,
                "previous_state": previous_state,
            }
        )

    signals = ado_kpi_stage.build_kusto_kpi_signals(
        queries=(
            KustoQuery(
                id="latency-p50",
                cluster="https://adventure.kusto.windows.net",
                database="xdataanalytics",
                kql="Metrics | take 1",
                section="Performance",
                render_as="metric_highlight",
                confidence="high",
                engine="kusto",
                result_column="P50",
            ),
        ),
        program=_program(),
        program_id="acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        workstreams=(),
        executor=lambda query: (_ for _ in ()).throw(RuntimeError("boom")),
        ado_client_factory=cast(Any, object),
        normalize_ado_team_name_fn=lambda value: value,
        record_kusto_query_state_fn=_record_kusto_state,
        build_kusto_kpi_signal_fn=_build_signal,
        summarize_pull_requests_fn=lambda **kwargs: None,
        pull_request_provider_ref_fn=lambda pull_request, repository_name: None,
        pull_request_entity_refs_fn=lambda pull_request, repository_name: (),
        previous_query_states={"latency-p50": {"value_last_4": [4.2]}},
    )

    assert signals == ()
    assert recorded == [
        {
            "query_id": "latency-p50",
            "rows": [],
            "error": "boom",
            "previous_state": {"value_last_4": [4.2]},
        }
    ]


def test_build_kusto_kpi_signals_marks_capped_wiql_result_degraded() -> None:
    """ADF-W2.1 (Section 8.4.2): the KPI stage's WIQL call path now applies the
    same cap_reached treatment the production gather WIQL path already had -- a
    WIQL result at exactly ADO_WIQL_DEFAULT_TOP surfaces as cap_reached=True /
    is_degraded=True through the query-state sink, not just a log line."""
    workstreams = (Workstream(id="acme", name="Acme", area_paths=("One\\Adventure\\Acme",)),)
    query_state_sink: dict[str, dict[str, Any]] = {}

    capped_ids = list(range(1, ADO_WIQL_DEFAULT_TOP + 1))

    class _CappedADOClient:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        def list_team_iterations(self, timeframe: str | None = None, team: str | None = None) -> list[dict[str, object]]:
            del timeframe, team
            return [{"path": "One\\Sprint 24"}]

        def execute_wiql(self, wiql: str, top: int | None = None) -> list[int]:
            del wiql, top
            return capped_ids

        def query_work_items_batch(self, work_item_ids: list[int], fields: tuple[str, ...]) -> list[dict[str, object]]:
            return [
                {"id": wid, "fields": {"System.Id": wid, "System.Title": f"Item {wid}", "System.State": "Active"}}
                for wid in work_item_ids
            ]

    ado_kpi_stage.build_kusto_kpi_signals(
        queries=(_wiql_query(query_id="capped-kpi"),),
        program=_program(),
        program_id="acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        workstreams=workstreams,
        executor=lambda query: [],
        ado_client_factory=cast(Any, _CappedADOClient),
        normalize_ado_team_name_fn=lambda value: None if value is None else value.strip() or None,
        record_kusto_query_state_fn=lambda *args, **kwargs: None,
        build_kusto_kpi_signal_fn=_build_signal,
        summarize_pull_requests_fn=lambda **kwargs: None,
        pull_request_provider_ref_fn=lambda pull_request, repository_name: None,
        pull_request_entity_refs_fn=lambda pull_request, repository_name: (),
        query_state_sink=query_state_sink,
    )

    state = query_state_sink["capped-kpi"]
    assert state["cap_reached"] is True
    assert state["is_degraded"] is True
    assert state["row_count"] == ADO_WIQL_DEFAULT_TOP


def test_build_kusto_kpi_signals_under_cap_is_not_degraded() -> None:
    """A WIQL result under the cap must NOT set cap_reached."""
    workstreams = (Workstream(id="acme", name="Acme", area_paths=("One\\Adventure\\Acme",)),)
    query_state_sink: dict[str, dict[str, Any]] = {}

    class _UnderCapADOClient:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        def list_team_iterations(self, timeframe: str | None = None, team: str | None = None) -> list[dict[str, object]]:
            del timeframe, team
            return [{"path": "One\\Sprint 24"}]

        def execute_wiql(self, wiql: str, top: int | None = None) -> list[int]:
            del wiql, top
            return [5001, 5002, 5003]

        def query_work_items_batch(self, work_item_ids: list[int], fields: tuple[str, ...]) -> list[dict[str, object]]:
            return [
                {"id": wid, "fields": {"System.Id": wid, "System.Title": f"Item {wid}", "System.State": "Active"}}
                for wid in work_item_ids
            ]

    ado_kpi_stage.build_kusto_kpi_signals(
        queries=(_wiql_query(query_id="under-cap-kpi"),),
        program=_program(),
        program_id="acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        workstreams=workstreams,
        executor=lambda query: [],
        ado_client_factory=cast(Any, _UnderCapADOClient),
        normalize_ado_team_name_fn=lambda value: None if value is None else value.strip() or None,
        record_kusto_query_state_fn=lambda *args, **kwargs: None,
        build_kusto_kpi_signal_fn=_build_signal,
        summarize_pull_requests_fn=lambda **kwargs: None,
        pull_request_provider_ref_fn=lambda pull_request, repository_name: None,
        pull_request_entity_refs_fn=lambda pull_request, repository_name: (),
        query_state_sink=query_state_sink,
    )

    state = query_state_sink["under-cap-kpi"]
    assert state["cap_reached"] is False
    assert state["row_count"] == 3
