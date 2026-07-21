from __future__ import annotations

from datetime import datetime, timezone

from src.core.ado_saved_query_helpers import (
    append_wiql_clause,
    bound_saved_query_wiql,
    load_saved_query_item_ids,
    query_work_item_batch_rows,
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


def test_bound_saved_query_wiql_skips_date_bound_when_since_is_none() -> None:
    """Armada spec D-2: a `full_scope` binding must never be date-bounded by a
    consumer -- passing since=None must leave the WIQL's membership identical
    to gather's undated saved-query execution (only filter/tag clauses may be
    appended)."""
    wiql = (
        "select [System.Id], [System.Title] from WorkItems "
        "where [System.TeamProject] = @project "
        "order by [System.Id]"
    )

    result = bound_saved_query_wiql(wiql, since=None, additional_clause=None)

    assert result == wiql
    assert "ChangedDate" not in result


def test_bound_saved_query_wiql_since_none_still_applies_additional_clause() -> None:
    wiql = "select [System.Id] from WorkItems where [System.TeamProject] = @project order by [System.Id]"

    result = bound_saved_query_wiql(wiql, since=None, additional_clause="[System.AreaPath] under 'One\\XCatalog'")

    assert "ChangedDate" not in result
    assert "[System.AreaPath] under 'One\\XCatalog'" in result


def test_load_saved_query_item_ids_since_none_produces_undated_wiql() -> None:
    class _FakeClient:
        def get_saved_query(self, query_id: str) -> dict[str, str]:
            return {
                "wiql": (
                    "select [System.Id] from WorkItems where [System.TeamProject] = 'One' "
                    f"and [System.AreaPath] under '{query_id}' order by [System.Id]"
                )
            }

        def execute_wiql(self, wiql: str, top: int = 2000) -> list[int]:
            assert "ChangedDate" not in wiql
            return [201, 202]

    ids, membership, ado_calls = load_saved_query_item_ids(
        _FakeClient(),
        ("full-scope-query",),
        since=None,
        top_cap=2000,
    )

    assert ids == [201, 202]
    assert membership == {201: ("full-scope-query",), 202: ("full-scope-query",)}
    assert ado_calls == 2


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


def test_query_work_item_batch_rows_happy_path_makes_one_call_per_batch() -> None:
    class _FakeClient:
        def __init__(self) -> None:
            self.calls: list[list[int]] = []

        def query_work_items_batch(self, work_item_ids: list[int], fields: tuple[str, ...]) -> list[dict[str, object]]:
            self.calls.append(list(work_item_ids))
            return [{"id": i} for i in work_item_ids]

    client = _FakeClient()
    rows, ado_calls = query_work_item_batch_rows(client, [1, 2, 3], ("System.Id",), batch_size=2)

    assert ado_calls == 2
    assert rows == [{"id": 1}, {"id": 2}, {"id": 3}]
    assert client.calls == [[1, 2], [3]]


def test_query_work_item_batch_rows_isolates_a_single_bad_id_via_per_item_fallback() -> None:
    """ADF-OM6: a single deleted/inaccessible id in a batch (Azure DevOps
    raises for permission-denied ids rather than silently omitting them)
    must not poison the rest of that batch -- reproduces the real Armada
    onboarding failure (work item 17177322: 404 WorkItemUnauthorizedAccessException)."""
    class _FakeClient:
        def __init__(self, bad_id: int) -> None:
            self.bad_id = bad_id
            self.calls: list[list[int]] = []

        def query_work_items_batch(self, work_item_ids: list[int], fields: tuple[str, ...]) -> list[dict[str, object]]:
            self.calls.append(list(work_item_ids))
            if self.bad_id in work_item_ids:
                raise QueryError(
                    f"ADO request failed with status 404: work item {self.bad_id} does not exist, "
                    "or you do not have permissions to read it."
                )
            return [{"id": i} for i in work_item_ids]

    client = _FakeClient(bad_id=17177322)
    rows, ado_calls = query_work_item_batch_rows(
        client, [36621625, 35156095, 17177322], ("System.Id",), batch_size=200
    )

    # The bad id is skipped; every good id in the same chunk still comes back.
    assert {row["id"] for row in rows} == {36621625, 35156095}
    # First call is the whole chunk (fails); then 3 individual retries.
    assert client.calls[0] == [36621625, 35156095, 17177322]
    assert client.calls[1:] == [[36621625], [35156095], [17177322]]
    assert ado_calls == 4  # 1 failed batch call + 3 individual retries


def test_query_work_item_batch_rows_all_ids_bad_returns_empty_not_raise() -> None:
    class _FakeClient:
        def query_work_items_batch(self, work_item_ids: list[int], fields: tuple[str, ...]) -> list[dict[str, object]]:
            raise QueryError("ADO request failed with status 404: not found")

    rows, ado_calls = query_work_item_batch_rows(_FakeClient(), [1, 2], ("System.Id",), batch_size=200)

    assert rows == []
    assert ado_calls == 3  # 1 failed batch + 2 individual retries, both fail
