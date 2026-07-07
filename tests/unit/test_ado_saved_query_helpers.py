from __future__ import annotations

from datetime import datetime, timezone

from src.core.ado_saved_query_helpers import (
    append_wiql_clause,
    bound_saved_query_wiql,
    load_saved_query_item_ids,
    query_work_item_snapshot_history_rows,
)
from src.core.exceptions import QueryError


def test_bound_saved_query_wiql_injects_date_when_changeddate_only_in_select() -> None:
    wiql = (
        "select [System.Id], [System.Title], [System.ChangedDate] from WorkItems "
        "where [System.TeamProject] = @project "
        "and [System.AreaPath] under 'One\\Adventure\\XDirect\\Scenarios' "
        "order by [System.ChangedDate] desc"
    )
    since = datetime(2026, 5, 8, tzinfo=timezone.utc)

    result = bound_saved_query_wiql(wiql, since=since, additional_clause=None)

    assert "[System.ChangedDate] >= '2026-05-08'" in result
    assert result.index("[System.ChangedDate] >= '2026-05-08'") < result.lower().index(" order by ")


def test_append_wiql_clause_wraps_discovery_clause_before_order_by() -> None:
    wiql = "Select [System.Id] From WorkItems order by [System.ChangedDate] desc"

    result = append_wiql_clause(wiql, "([System.Tags] Contains Words 'RAMPP1')")

    assert result == "Select [System.Id] From WorkItems where (([System.Tags] Contains Words 'RAMPP1')) order by [System.ChangedDate] desc"


def test_load_saved_query_item_ids_skips_failing_query_and_returns_rest() -> None:
    class _FakeClient:
        def get_saved_query(self, query_id: str) -> dict[str, str]:
            return {
                "wiql": (
                    "select [System.Id] from WorkItems where [System.TeamProject] = 'One' "
                    f"and [System.AreaPath] under '{query_id}'"
                )
            }

        def execute_wiql(self, wiql: str, top: int = 2000) -> list[int]:
            assert top == 2000
            if "bad-query" in wiql:
                raise QueryError("ADO request failed with status 408: timeout")
            return [101, 102]

    ids, membership, ado_calls = load_saved_query_item_ids(
        _FakeClient(),
        ("bad-query", "good-query"),
        since=datetime(2026, 5, 8, tzinfo=timezone.utc),
        top_cap=2000,
    )

    assert ids == [101, 102]
    assert membership == {101: ("good-query",), 102: ("good-query",)}
    assert ado_calls == 4


def test_query_work_item_snapshot_history_rows_batches_calls() -> None:
    class _FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[list[int], tuple[str, ...], str | None]] = []

        def query_work_item_snapshot_history(
            self,
            work_item_ids: list[int],
            *,
            select_fields: tuple[str, ...],
            start_date: str | None = None,
        ) -> list[dict[str, object]]:
            self.calls.append((list(work_item_ids), select_fields, start_date))
            return [{"WorkItemId": work_item_ids[0]}]

    client = _FakeClient()

    rows, ado_calls = query_work_item_snapshot_history_rows(
        client,
        [1, 2, 3],
        select_fields=("WorkItemId",),
        start_date="2026-01-01",
        batch_size=2,
    )

    assert ado_calls == 2
    assert rows == [{"WorkItemId": 1}, {"WorkItemId": 3}]
    assert client.calls == [
        ([1, 2], ("WorkItemId",), "2026-01-01"),
        ([3], ("WorkItemId",), "2026-01-01"),
    ]
