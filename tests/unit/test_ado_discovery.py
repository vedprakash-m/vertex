from __future__ import annotations

from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest
import yaml

from src.core.ado_discovery import (
    ADODiscoveryConfig,
    ADODiscoveryProvider,
    LinkedItemExpansionResult,
    _discovery_scopes,
    _execute_wiql_with_id_partitioning,
    expand_with_linked_items,
)
from src.core.integration_types import DiscoveryCompleteness, PaginationOutcome, ScopeStatusKind
from src.core.slice_contract_loader import SliceContract, load_slice_contract


CURRENT_QUERY = str(uuid5(NAMESPACE_URL, "https://example.test/fixtures/discovery/current"))
HISTORY_QUERY = str(uuid5(NAMESPACE_URL, "https://example.test/fixtures/discovery/history"))


class _FakeADOClient:
    def __init__(self) -> None:
        self.executed_wiql: list[str] = []

    def get_saved_query(self, query_id: str) -> dict[str, object]:
        return {"id": query_id, "wiql": "Select [System.Id] From WorkItems"}

    def execute_wiql(self, wiql: str, top: int | None = None, *, on_pagination=None) -> list[int]:
        self.executed_wiql.append(wiql)
        assert top == 10000
        ids = [101, 102]
        if on_pagination is not None:
            from src.core.integration_types import PaginationOutcome

            on_pagination(PaginationOutcome(total_fetched=len(ids), page_count=1, is_truncated=len(ids) >= top))
        return ids


def test_ado_discovery_uses_tag_expression_and_explicit_ids(tmp_path: Path) -> None:
    contract_path = tmp_path / "slice_contracts.yaml"
    contract_path.write_text(
        "\n".join(
            [
                'schema_version: "1.0"',
                "slices:",
                "  - id: demo.slice",
                '    scorecard_name: "Demo"',
                "    section: demo",
                "    workstream: Demo",
                "    slice_kind: scorecard_dimension",
                '    title: "Deployment"',
                "    source_of_truth: ado_primary",
                "    owners:",
                '      primary: "Owner"',
                "    source_contract:",
                "      ado:",
                "        saved_queries:",
                "          - query-1",
                "        tag_expression:",
                "          all_of: [ExampleRelease]",
                "        explicit_work_item_ids: [999]",
                "        required_fields: [state]",
                "    freshness:",
                "      warn_days: 5",
                "      block_days: 10",
                "    degradation:",
                "      blank_filter_is_error: true",
            ]
        ),
        encoding="utf-8",
    )
    client = _FakeADOClient()
    provider = ADODiscoveryProvider(client)  # type: ignore[arg-type]

    result = provider.discover("demo", ADODiscoveryConfig(load_slice_contract(contract_path)), ())

    assert result.completeness is DiscoveryCompleteness.FULL
    assert [ref.registration.ref_id for ref in result.discovered_refs] == ["101", "102", "999"]
    assert result.discovered_refs[0].bindings[0].workstream_id == "demo.slice"
    assert client.executed_wiql == [
        "Select [System.Id] From WorkItems where (([System.Tags] Contains Words 'ExampleRelease'))"
    ]
    assert len(result.query_results) == 1
    query_result = result.query_results[0]
    assert query_result.query_id == "query-1"
    assert query_result.scope_id == "demo.slice:query-1"
    assert query_result.membership_ids == ("101", "102")
    assert query_result.raw_count == 2
    assert query_result.completeness_state == "FULL"
    assert query_result.cap_reached is False
    assert len(query_result.wiql_hash) == 64
    assert len(query_result.membership_hash) == 64


def test_ado_discovery_deduplicates_multi_scope_refs(tmp_path: Path) -> None:
    contract_path = tmp_path / "slice_contracts.yaml"
    contract_path.write_text(
        "\n".join(
            [
                'schema_version: "1.0"',
                "slices:",
                "  - id: demo.a",
                '    scorecard_name: "Demo"',
                "    section: demo",
                "    workstream: Demo",
                "    slice_kind: scorecard_dimension",
                '    title: "A"',
                "    source_of_truth: ado_primary",
                "    owners:",
                '      primary: "Owner"',
                "    source_contract:",
                "      ado:",
                "        saved_queries: [query-1]",
                "        tag_expression: {all_of: [ExampleRelease]}",
                "        explicit_work_item_ids: []",
                "        required_fields: [state]",
                "    freshness: {warn_days: 5, block_days: 10}",
                "    degradation: {blank_filter_is_error: true}",
                "  - id: demo.b",
                '    scorecard_name: "Demo"',
                "    section: demo",
                "    workstream: Demo",
                "    slice_kind: scorecard_dimension",
                '    title: "B"',
                "    source_of_truth: ado_primary",
                "    owners:",
                '      primary: "Owner"',
                "    source_contract:",
                "      ado:",
                "        saved_queries: [query-2]",
                "        tag_expression: {all_of: [ExampleRelease]}",
                "        explicit_work_item_ids: []",
                "        required_fields: [state]",
                "    freshness: {warn_days: 5, block_days: 10}",
                "    degradation: {blank_filter_is_error: true}",
            ]
        ),
        encoding="utf-8",
    )
    provider = ADODiscoveryProvider(_FakeADOClient())  # type: ignore[arg-type]

    result = provider.discover("demo", ADODiscoveryConfig(load_slice_contract(contract_path)), ())

    assert [ref.registration.ref_id for ref in result.discovered_refs] == ["101", "102"]
    assert {binding.workstream_id for binding in result.discovered_refs[0].bindings} == {"demo.a", "demo.b"}


def test_ado_discovery_partial_failure_preserves_successful_scope_refs(tmp_path: Path) -> None:
    """When one scope raises QueryError, other scopes' refs are preserved and completeness is PARTIAL."""
    from src.core.exceptions import QueryError
    from src.core.integration_types import ScopeStatusKind

    contract_path = tmp_path / "slice_contracts.yaml"
    contract_path.write_text(
        "\n".join([
            'schema_version: "1.0"',
            "slices:",
            "  - id: demo.ok",
            '    scorecard_name: "Demo"',
            "    section: demo",
            "    workstream: Demo",
            "    slice_kind: scorecard_dimension",
            '    title: "Ok"',
            "    source_of_truth: ado_primary",
            "    owners: {primary: Owner}",
            "    source_contract:",
            "      ado:",
            "        saved_queries: [query-ok]",
            "        tag_expression: {all_of: [ExampleRelease]}",
            "        explicit_work_item_ids: []",
            "        required_fields: [state]",
            "    freshness: {warn_days: 5, block_days: 10}",
            "    degradation: {blank_filter_is_error: true}",
            "  - id: demo.fail",
            '    scorecard_name: "Demo"',
            "    section: demo",
            "    workstream: Demo",
            "    slice_kind: scorecard_dimension",
            '    title: "Fail"',
            "    source_of_truth: ado_primary",
            "    owners: {primary: Owner}",
            "    source_contract:",
            "      ado:",
            "        saved_queries: [query-fail]",
            "        tag_expression: {all_of: [ExampleRelease]}",
            "        explicit_work_item_ids: []",
            "        required_fields: [state]",
            "    freshness: {warn_days: 5, block_days: 10}",
            "    degradation: {blank_filter_is_error: true}",
        ]),
        encoding="utf-8",
    )

    class _PartialFailClient(_FakeADOClient):
        def get_saved_query(self, query_id: str) -> dict[str, object]:
            if query_id == "query-fail":
                raise QueryError("scope-fail timed out")
            return {"id": query_id, "wiql": f"Select [System.Id] From WorkItems WHERE query = '{query_id}'"}

    provider = ADODiscoveryProvider(_PartialFailClient())  # type: ignore[arg-type]

    result = provider.discover("demo", ADODiscoveryConfig(load_slice_contract(contract_path)), ())

    assert result.completeness is DiscoveryCompleteness.PARTIAL
    # At least the successful scope's refs are present
    assert len(result.discovered_refs) >= 1
    # The failing scope has ERROR scope status (scope_id = "{slice_id}:{query_id}")
    failing_scope = result.scope_statuses.get("demo.fail:query-fail")
    assert failing_scope is not None
    assert failing_scope.status is ScopeStatusKind.ERROR


@pytest.mark.parametrize("query_payload", [{}, {"wiql": None}, {"wiql": ""}, {"wiql": " \n\t"}, {"wiql": 123}])
def test_missing_wiql_is_error_for_grouped_scopes_without_losing_siblings(
    tmp_path: Path, query_payload: dict[str, object],
) -> None:
    from src.core.integration_types import ScopeStatusKind

    slices = [
        {
            "id": slice_id, "scorecard_name": "Demo", "section": "demo", "workstream": "Demo",
            "slice_kind": "scorecard_dimension", "title": slice_id, "source_of_truth": "ado_primary",
            "owners": {"primary": "Owner"},
            "source_contract": {"ado": {
                "saved_queries": [query_id], "tag_expression": {"all_of": ["ExampleRelease"]},
                "explicit_work_item_ids": [999], "required_fields": ["state"],
            }},
            "freshness": {"warn_days": 5, "block_days": 10},
            "degradation": {"blank_filter_is_error": True},
        }
        for slice_id, query_id in (
            ("demo.fail_a", "query-fail"), ("demo.fail_b", "query-fail"), ("demo.ok", "query-ok"),
        )
    ]
    contract_path = tmp_path / "slice_contracts.yaml"
    contract_path.write_text(yaml.safe_dump({"schema_version": "1.0", "slices": slices}), encoding="utf-8")

    class _MissingWIQLClient(_FakeADOClient):
        def get_saved_query(self, query_id: str) -> dict[str, object]:
            return query_payload if query_id == "query-fail" else super().get_saved_query(query_id)

    client = _MissingWIQLClient()
    result = ADODiscoveryProvider(client).discover(  # type: ignore[arg-type]
        "demo", ADODiscoveryConfig(load_slice_contract(contract_path)), (),
    )

    assert result.completeness is DiscoveryCompleteness.PARTIAL
    assert [ref.registration.ref_id for ref in result.discovered_refs] == ["101", "102", "999"]
    assert len(client.executed_wiql) == 1
    for slice_id in ("demo.fail_a", "demo.fail_b"):
        scope_id = f"{slice_id}:query-fail"
        status = result.scope_statuses[scope_id]
        assert status.status is ScopeStatusKind.ERROR
        assert status.completeness is DiscoveryCompleteness.PARTIAL
        assert status.item_count == 0
        assert status.error_message is not None and "WIQL" in status.error_message
        row = next(row for row in result.query_results if row.scope_id == scope_id)
        assert row.completeness_state == "ERROR"
        assert row.failure_category == "missing_wiql"
        assert row.wiql_hash == ""
        assert row.membership_ids == () and row.raw_count == 0
    assert result.scope_statuses["demo.ok:query-ok"].status is ScopeStatusKind.SUCCESS
    assert result.scope_statuses["demo.ok:query-ok"].completeness is DiscoveryCompleteness.FULL
    successful = next(row for row in result.query_results if row.query_id == "query-ok")
    assert successful.completeness_state == "FULL" and len(successful.wiql_hash) == 64


@pytest.mark.parametrize(
    "outcomes",
    [
        (),
        (object(),),
        (PaginationOutcome(1, 1, False), PaginationOutcome(1, 1, False)),
        (PaginationOutcome(0, 1, False),),
        (PaginationOutcome(0, 1, True),),
        (PaginationOutcome(True, 1, False),),
        (PaginationOutcome(1, 0, False),),
        (PaginationOutcome(1, True, False),),
        (PaginationOutcome(1, 1, 0),),
        (PaginationOutcome(1, 1, 1),),
        (PaginationOutcome(1, 1, "false"),),
    ],
)
def test_unverified_wiql_pagination_preserves_partial_membership_and_registry(
    tmp_path: Path, outcomes: tuple[object, ...],
) -> None:
    from src.core.channel_registry_store import ChannelRegistryStore

    class Client(_FakeADOClient):
        ids = [101, 102]
        pagination: tuple[object, ...] = (PaginationOutcome(2, 1, False),)

        def execute_wiql(self, wiql: str, top: int | None = None, *, on_pagination=None) -> list[int]:
            self.executed_wiql.append(wiql)
            if on_pagination is not None:
                for outcome in self.pagination:
                    on_pagination(outcome)
            return self.ids

    client = Client()
    provider = ADODiscoveryProvider(client)  # type: ignore[arg-type]
    config = _pagination_config(tmp_path, ["query-1"])
    previous = provider.discover("demo", config, ())
    assert previous.completeness is DiscoveryCompleteness.FULL
    store = ChannelRegistryStore(tmp_path / "registry.sqlite", "demo")
    store.apply_discovery_result(previous)

    client.ids = [101]
    client.pagination = outcomes
    result = provider.discover("demo", config, previous.discovered_refs)
    delta = store.apply_discovery_result(result)

    assert delta.removed == ()
    assert {item.ref_id for item in store.active_registrations("ado")} == {"101", "102"}
    assert result.completeness is DiscoveryCompleteness.PARTIAL
    assert [ref.registration.ref_id for ref in result.discovered_refs] == ["101"]
    status = result.scope_statuses["demo.slice:query-1"]
    assert status.status is ScopeStatusKind.ERROR
    assert status.completeness is DiscoveryCompleteness.PARTIAL
    assert status.error_message is not None and "pagination" in status.error_message.lower()
    row = result.query_results[0]
    assert row.membership_ids == ("101",)
    assert row.completeness_state == "PARTIAL"
    assert row.failure_category == "unverified_pagination"
    assert row.cap_reached is False
    assert len(client.executed_wiql) == 2


def test_unverified_wiql_pagination_preserves_independent_scope_retirement(tmp_path: Path) -> None:
    from src.core.channel_registry_store import ChannelRegistryStore

    class Client(_FakeADOClient):
        second_run = False

        def get_saved_query(self, query_id: str) -> dict[str, object]:
            lower_bound = 1 if query_id == "good" else 2
            return {"wiql": f"Select [System.Id] From WorkItems where [System.Id] >= {lower_bound}"}

        def execute_wiql(self, wiql: str, top: int | None = None, *, on_pagination=None) -> list[int]:
            good = "[System.Id] >= 1" in wiql
            ids = ([101] if good else [202]) if self.second_run else (
                [101, 102, 300] if good else [201, 202, 300]
            )
            if on_pagination is not None and (good or not self.second_run):
                on_pagination(PaginationOutcome(len(ids), 1, False))
            return ids

    client = Client()
    provider = ADODiscoveryProvider(client)  # type: ignore[arg-type]
    config = _pagination_config(tmp_path, ["good", "unverified"])
    previous = provider.discover("demo", config, ())
    assert previous.completeness is DiscoveryCompleteness.FULL
    store = ChannelRegistryStore(tmp_path / "registry.sqlite", "demo")
    store.apply_discovery_result(previous)

    client.second_run = True
    result = provider.discover("demo", config, previous.discovered_refs)
    delta = store.apply_discovery_result(result)

    assert {item.ref_id for item in delta.removed} == {"102"}
    assert {item.ref_id for item in store.active_registrations("ado")} == {"101", "201", "202", "300"}
    assert result.scope_statuses["demo.slice:good"].status is ScopeStatusKind.SUCCESS
    assert result.scope_statuses["demo.slice:unverified"].status is ScopeStatusKind.ERROR
    assert result.completeness is DiscoveryCompleteness.PARTIAL


def test_unverified_wiql_pagination_marks_every_grouped_scope(tmp_path: Path) -> None:
    slices = [
        {
            "id": slice_id, "scorecard_name": "Demo", "section": "demo", "workstream": "Demo",
            "slice_kind": "scorecard_dimension", "title": slice_id, "source_of_truth": "ado_primary",
            "owners": {"primary": "Owner"},
            "source_contract": {"ado": {
                "saved_queries": ["shared"], "tag_expression": {"all_of": ["Release"]},
                "explicit_work_item_ids": [999], "required_fields": ["state"],
            }},
            "freshness": {"warn_days": 5, "block_days": 10},
            "degradation": {"blank_filter_is_error": True},
        }
        for slice_id in ("demo.a", "demo.b")
    ]
    contract_path = tmp_path / "slice_contracts.yaml"
    contract_path.write_text(yaml.safe_dump({"schema_version": "1.0", "slices": slices}), encoding="utf-8")

    class Client(_FakeADOClient):
        def execute_wiql(self, wiql: str, top: int | None = None, *, on_pagination=None) -> list[int]:
            self.executed_wiql.append(wiql)
            return [101, 102]

    client = Client()
    result = ADODiscoveryProvider(client).discover(  # type: ignore[arg-type]
        "demo", ADODiscoveryConfig(load_slice_contract(contract_path)), (),
    )
    assert result.completeness is DiscoveryCompleteness.PARTIAL
    assert [ref.registration.ref_id for ref in result.discovered_refs] == ["101", "102", "999"]
    assert {binding.scope_id for binding in result.discovered_refs[0].bindings} == {
        "demo.a:shared", "demo.b:shared",
    }
    assert len(client.executed_wiql) == 1
    for row in result.query_results:
        assert row.failure_category == "unverified_pagination" and row.cap_reached is False
        assert result.scope_statuses[row.scope_id].status is ScopeStatusKind.ERROR
        assert result.scope_statuses[row.scope_id].completeness is DiscoveryCompleteness.PARTIAL


@pytest.mark.parametrize("ids", [[], [101]])
@pytest.mark.parametrize("page_count", [1, 2])
def test_verified_wiql_pagination_preserves_empty_and_populated_results(
    tmp_path: Path, ids: list[int], page_count: int,
) -> None:
    class Client(_FakeADOClient):
        def execute_wiql(self, wiql: str, top: int | None = None, *, on_pagination=None) -> list[int]:
            if on_pagination is not None:
                on_pagination(PaginationOutcome(len(ids), page_count, False))
            return ids

    result = ADODiscoveryProvider(Client()).discover(  # type: ignore[arg-type]
        "demo", _pagination_config(tmp_path, ["query-1"]), (),
    )
    assert result.completeness is DiscoveryCompleteness.FULL
    assert result.scope_statuses["demo.slice:query-1"].status is ScopeStatusKind.SUCCESS
    assert result.query_results[0].membership_ids == tuple(str(item) for item in ids)
    assert result.query_results[0].failure_category is None


def _pagination_config(tmp_path: Path, queries: list[str]) -> ADODiscoveryConfig:
    contract_path = tmp_path / "slice_contracts.yaml"
    contract_path.write_text(
        'schema_version: "1.0"\nslices:\n'
        "  - id: demo.slice\n    scorecard_name: Demo\n    section: demo\n"
        "    workstream: Demo\n    slice_kind: scorecard_dimension\n    title: Delivery\n"
        "    source_of_truth: ado_primary\n    owners: {primary: Owner}\n"
        f"    source_contract:\n      ado:\n        saved_queries: [{', '.join(queries)}]\n"
        "        tag_expression: {all_of: [Release]}\n        required_fields: [state]\n"
        "    freshness: {warn_days: 5, block_days: 10}\n"
        "    degradation: {blank_filter_is_error: true}\n",
        encoding="utf-8",
    )
    return ADODiscoveryConfig(load_slice_contract(contract_path))


@pytest.mark.parametrize(
    "page, outcomes",
    [
        pytest.param([4], (), id="missing"),
        pytest.param([4], (object(),), id="wrong-outcome-type"),
        pytest.param([4], (PaginationOutcome(1, 1, False),) * 2, id="multiple-outcomes"),
        pytest.param([4], (PaginationOutcome(2, 1, False),), id="count-mismatch"),
        pytest.param([4], (PaginationOutcome(True, 1, False),), id="boolean-count"),
        pytest.param([4], (PaginationOutcome(1, 0, False),), id="zero-pages"),
        pytest.param([4], (PaginationOutcome(1, 2, False),), id="multiple-pages"),
        pytest.param([4], (PaginationOutcome(1, True, False),), id="boolean-page-count"),
        pytest.param([4], (PaginationOutcome(1, 1, 0),), id="nonboolean-truncated"),
        pytest.param([4], (PaginationOutcome(1, 1, True),), id="short-truncated-page"),
        pytest.param([], (PaginationOutcome(0, 1, True),), id="empty-truncated-page"),
        pytest.param([4, 5, 6], (PaginationOutcome(3, 1, False),), id="untruncated-full-page"),
        pytest.param([4, 5, 6, 7], (PaginationOutcome(4, 1, True),), id="over-cap-page"),
        pytest.param([5, 4], (PaginationOutcome(2, 1, False),), id="descending-page"),
        pytest.param([3, 4], (PaginationOutcome(2, 1, False),), id="repeated-cursor"),
        pytest.param([4, 4], (PaginationOutcome(2, 1, False),), id="repeated-id"),
        pytest.param([0], (PaginationOutcome(1, 1, False),), id="zero-id"),
        pytest.param([-1], (PaginationOutcome(1, 1, False),), id="negative-id"),
        pytest.param([True], (PaginationOutcome(1, 1, False),), id="boolean-id"),
        pytest.param(["4"], (PaginationOutcome(1, 1, False),), id="string-id"),
    ],
)
def test_partition_scan_rejects_inconsistent_pages(page: list[object], outcomes: tuple[object, ...]) -> None:
    class Client:
        calls = 0

        def execute_wiql(self, wiql, top=None, *, on_pagination=None):
            self.calls += 1
            if self.calls == 1:
                ids, evidence = [1, 2, 3], (PaginationOutcome(3, 1, True),)
            elif self.calls == 2:
                ids, evidence = page, outcomes
            else:
                ids, evidence = [], (PaginationOutcome(0, 1, False),)
            if on_pagination is not None:
                for outcome in evidence:
                    on_pagination(outcome)
            return ids

    result = _execute_wiql_with_id_partitioning(Client(), "Select [System.Id] From WorkItems", 3)

    expected = [1, 2, 3]
    for item in page:
        if type(item) is int and item > 0 and item not in expected:
            expected.append(item)
    assert result[0] == expected
    assert result[1] is False


@pytest.mark.parametrize(
    "pages, expected",
    [
        ([[]], []),
        ([[1, 2, 3], [4]], [1, 2, 3, 4]),
        ([[1, 2, 3], [4, 5, 6], []], [1, 2, 3, 4, 5, 6]),
    ],
)
def test_partition_scan_accepts_verified_ordered_pages(pages: list[list[int]], expected: list[int]) -> None:
    class Client:
        def execute_wiql(self, wiql, top=None, *, on_pagination=None):
            ids = pages.pop(0)
            if on_pagination is not None:
                on_pagination(PaginationOutcome(len(ids), 1, len(ids) == top))
            return ids

    result = _execute_wiql_with_id_partitioning(Client(), "Select [System.Id] From WorkItems", 3)
    assert result[0] == expected
    assert result[1] is True
    assert pages == []


def test_partition_scan_accepts_real_client_pagination_without_network(tmp_path: Path) -> None:
    from src.core.ado_client import ADOClient

    class Client(ADOClient):
        def __init__(self):
            self._rest_base_url = "https://example.invalid/"
            self.pages = [[2, 1, 3], [1, 2, 3], [4]]

        def get_saved_query(self, query_id):
            return {"wiql": "Select [System.Id] From WorkItems"}

        def _request_json(self, method, url, **kwargs):
            return {"workItems": [{"id": item} for item in self.pages.pop(0)]}

    client = Client()
    base = _pagination_config(tmp_path, ["query"])
    result = ADODiscoveryProvider(client).discover(
        "demo", ADODiscoveryConfig(base.slice_contracts, top=3), (),
    )

    assert client.pages == []
    assert result.completeness is DiscoveryCompleteness.FULL
    assert result.query_results[0].membership_ids == ("1", "2", "3", "4")
    assert result.query_results[0].cap_reached is False


@pytest.mark.parametrize("top_cap", [0, -1, True])
def test_partition_scan_rejects_invalid_cap(top_cap: int) -> None:
    class Client:
        def execute_wiql(self, wiql, top=None, *, on_pagination=None):
            raise AssertionError("invalid cap must not start a scan")

    result = _execute_wiql_with_id_partitioning(Client(), "Select [System.Id] From WorkItems", top_cap)
    assert result[0] == []
    assert result[1] is False


@pytest.mark.parametrize("terminal", [[], [4]])
@pytest.mark.parametrize(("elapsed", "complete"), [(300.0, True), (300.001, False)])
def test_partition_scan_checks_terminal_page_skew(terminal: list[int], elapsed: float, complete: bool) -> None:
    pages = [[1, 2, 3], terminal]
    times = iter((0.0, 1.0, elapsed))

    class Client:
        def execute_wiql(self, wiql, top=None, *, on_pagination=None):
            ids = pages.pop(0)
            if on_pagination is not None:
                on_pagination(PaginationOutcome(len(ids), 1, len(ids) == top))
            return ids

    result = _execute_wiql_with_id_partitioning(
        Client(), "Select [System.Id] From WorkItems", 3, clock=lambda: next(times),
    )
    assert result[0] == [1, 2, 3, *terminal]
    assert result[1] is complete


@pytest.mark.parametrize("mode", ["missing-evidence", "query-error", "contradictory-initial"])
def test_partition_scan_partial_preserves_observations_and_registry(tmp_path: Path, mode: str) -> None:
    from src.core.channel_registry_store import ChannelRegistryStore
    from src.core.exceptions import QueryError

    class Client(_FakeADOClient):
        second_run = False

        def get_saved_query(self, query_id: str) -> dict[str, object]:
            lower_bound = 10000 if query_id == "good" else 20000
            return {"wiql": f"Select [System.Id] From WorkItems where [System.Id] >= {lower_bound}"}

        def execute_wiql(self, wiql: str, top: int | None = None, *, on_pagination=None) -> list[int]:
            if "[System.Id] >= 10000" in wiql:
                ids = [50] if self.second_run else [50, 51]
            elif not self.second_run:
                ids = [200]
            elif "[System.Id] > 0" in wiql:
                ids = [1, 2, 3]
            elif "[System.Id] > " in wiql:
                if mode == "query-error":
                    raise QueryError("synthetic private provider detail")
                ids = [4]
                if mode == "missing-evidence":
                    return ids
            else:
                ids = [90, 100, 110]
            if on_pagination is not None:
                on_pagination(PaginationOutcome(len(ids), 1, len(ids) == top))
            return ids

    client = Client()
    base = _pagination_config(tmp_path, ["good", "bad"])
    config = ADODiscoveryConfig(base.slice_contracts, top=3)
    provider = ADODiscoveryProvider(client)  # type: ignore[arg-type]
    previous = provider.discover("demo", config, ())
    assert previous.completeness is DiscoveryCompleteness.FULL
    store = ChannelRegistryStore(tmp_path / "registry.sqlite", "demo")
    store.apply_discovery_result(previous)

    client.second_run = True
    result = provider.discover("demo", config, ())
    delta = store.apply_discovery_result(result)

    observed = {1, 2, 3, 90, 100, 110} | ({4} if mode != "query-error" else set())
    assert {ref.registration.ref_id for ref in result.discovered_refs} == {"50"} | {str(i) for i in observed}
    assert {item.ref_id for item in delta.removed} == {"51"}
    assert {item.ref_id for item in store.active_registrations("ado")} == {"50", "200"} | {
        str(i) for i in observed
    }
    assert result.completeness is DiscoveryCompleteness.PARTIAL
    assert result.scope_statuses["demo.slice:good"].completeness is DiscoveryCompleteness.FULL
    status = result.scope_statuses["demo.slice:bad"]
    assert status.completeness is DiscoveryCompleteness.PARTIAL
    assert status.status is (ScopeStatusKind.ERROR if mode == "query-error" else ScopeStatusKind.SUCCESS)
    assert status.error_message is not None and "private provider detail" not in status.error_message
    row = next(row for row in result.query_results if row.query_id == "bad")
    assert set(row.membership_ids) == {str(i) for i in observed}
    assert row.cap_reached is True
    assert row.failure_category == ("query_error" if mode == "query-error" else None)


@pytest.mark.parametrize("mode", ["initial", "partition"])
def test_malformed_wiql_preserves_observations_and_registry(tmp_path: Path, mode: str) -> None:
    from src.core.ado_client import ADOClient
    from src.core.channel_registry_store import ChannelRegistryStore

    class Client(ADOClient):
        second_run = False

        def __init__(self) -> None:
            self._rest_base_url = "https://example.invalid/"

        def get_saved_query(self, query_id: str) -> dict[str, object]:
            lower_bound = 10000 if query_id == "good" else 20000
            return {"wiql": f"Select [System.Id] From WorkItems where [System.Id] >= {lower_bound}"}

        def _request_json(self, method: str, url: str, **kwargs: object) -> dict[str, object]:
            wiql = kwargs["json"]["query"]
            if "[System.Id] >= 10000" in wiql:
                ids = [50] if self.second_run else [50, 51]
            elif not self.second_run:
                ids = [200, 210]
            elif mode == "initial":
                return {"workItems": [{"id": 200}, {"id": "private-value"}, {"id": 201}]}
            elif "[System.Id] > 0" in wiql:
                ids = [1, 2, 3]
            elif "[System.Id] > " in wiql:
                return {"workItems": [{"id": 4}, {"id": "private-value"}, {"id": 5}]}
            else:
                ids = [90, 100, 110]
            return {"workItems": [{"id": item} for item in ids]}

    client = Client()
    base = _pagination_config(tmp_path, ["good", "bad"])
    config = ADODiscoveryConfig(base.slice_contracts, top=3)
    provider = ADODiscoveryProvider(client)
    previous = provider.discover("demo", config, ())
    assert previous.completeness is DiscoveryCompleteness.FULL
    store = ChannelRegistryStore(tmp_path / "registry.sqlite", "demo")
    store.apply_discovery_result(previous)

    client.second_run = True
    result = provider.discover("demo", config, ())
    delta = store.apply_discovery_result(result)

    observed = {200, 201} if mode == "initial" else {1, 2, 3, 4, 5, 90, 100, 110}
    assert {ref.registration.ref_id for ref in result.discovered_refs} == {"50"} | {str(i) for i in observed}
    assert {item.ref_id for item in delta.removed} == {"51"}
    assert {item.ref_id for item in store.active_registrations("ado")} == {"50", "200", "210"} | {
        str(i) for i in observed
    }
    assert result.completeness is DiscoveryCompleteness.PARTIAL
    assert result.scope_statuses["demo.slice:good"].completeness is DiscoveryCompleteness.FULL
    status = result.scope_statuses["demo.slice:bad"]
    assert status.status is ScopeStatusKind.ERROR
    assert status.completeness is DiscoveryCompleteness.PARTIAL
    assert status.error_message is not None and "private-value" not in status.error_message
    row = next(row for row in result.query_results if row.query_id == "bad")
    assert set(row.membership_ids) == {str(i) for i in observed}
    assert row.completeness_state == "ERROR"
    assert row.cap_reached is (mode == "partition")
    assert row.failure_category == ("malformed_wiql_response" if mode == "initial" else "query_error")
    assert len(row.wiql_hash) == 64


def test_malformed_wiql_ignores_prior_pagination_callback(tmp_path: Path) -> None:
    from src.core.exceptions import WIQLResponseError

    class Client(_FakeADOClient):
        def execute_wiql(self, wiql: str, top: int | None = None, *, on_pagination=None) -> list[int]:
            self.executed_wiql.append(wiql)
            assert "[System.Id] > " not in wiql
            if on_pagination is not None:
                on_pagination(PaginationOutcome(3, 1, True))
            raise WIQLResponseError((101, 102, 103))

    client = Client()
    base = _pagination_config(tmp_path, ["query"])
    result = ADODiscoveryProvider(client).discover(  # type: ignore[arg-type]
        "demo", ADODiscoveryConfig(base.slice_contracts, top=3), (),
    )

    assert len(client.executed_wiql) == 1
    assert result.completeness is DiscoveryCompleteness.PARTIAL
    assert result.scope_statuses["demo.slice:query"].status is ScopeStatusKind.ERROR
    row = result.query_results[0]
    assert row.membership_ids == ("101", "102", "103")
    assert row.completeness_state == "ERROR"
    assert row.failure_category == "malformed_wiql_response"
    assert row.cap_reached is False


@pytest.mark.parametrize("with_metadata", [False, True])
def test_capped_link_result_preserves_registry_without_flat_partition_scan(
    tmp_path: Path, with_metadata: bool,
) -> None:
    from src.core.ado_client import ADOClient
    from src.core.channel_registry_store import ChannelRegistryStore

    class Client(ADOClient):
        second_run = False

        def __init__(self) -> None:
            self._rest_base_url = "https://example.invalid/"
            self.queries: list[str] = []

        def get_saved_query(self, query_id: str) -> dict[str, object]:
            return {"wiql": "Select [System.Id] From WorkItemLinks"}

        def _request_json(self, method: str, url: str, **kwargs: object) -> dict[str, object]:
            wiql = kwargs["json"]["query"]
            self.queries.append(wiql)
            assert "[System.Id] > " not in wiql
            rows = (
                [{"source": {"id": 200}, "target": {"id": 201}},
                 {"source": {"id": 200}, "target": {"id": 201}},
                 {"source": {"id": 201}, "target": {"id": 200}}]
                if self.second_run else [{"source": {"id": 200}, "target": {"id": 210}}]
            )
            result = {"workItemRelations": rows}
            if with_metadata:
                result.update({"queryType": "oneHop", "queryResultType": "workItemLink"})
            return result

    client = Client()
    base = _pagination_config(tmp_path, ["link"])
    config = ADODiscoveryConfig(base.slice_contracts, top=3)
    provider = ADODiscoveryProvider(client)
    previous = provider.discover("demo", config, ())
    assert previous.completeness is DiscoveryCompleteness.FULL
    store = ChannelRegistryStore(tmp_path / "registry.sqlite", "demo")
    store.apply_discovery_result(previous)

    client.second_run = True
    result = provider.discover("demo", config, ())
    delta = store.apply_discovery_result(result)

    assert len(client.queries) == 2
    assert {ref.registration.ref_id for ref in result.discovered_refs} == {"200", "201"}
    assert not delta.removed
    assert {item.ref_id for item in store.active_registrations("ado")} == {"200", "201", "210"}
    assert result.completeness is DiscoveryCompleteness.PARTIAL
    assert result.scope_statuses["demo.slice:link"].completeness is DiscoveryCompleteness.PARTIAL
    row = result.query_results[0]
    assert row.cap_reached is True and row.completeness_state == "PARTIAL"
    assert set(row.membership_ids) == {"200", "201"}


@pytest.mark.parametrize(
    "terminal",
    [
        {"queryType": "oneHop", "workItemRelations": []},
        {"workItems": [], "workItemRelations": []},
    ],
)
def test_link_shaped_terminal_partition_page_cannot_prove_flat_scan_complete(
    tmp_path: Path, terminal: dict[str, object],
) -> None:
    from src.core.ado_client import ADOClient

    class Client(ADOClient):
        def __init__(self) -> None:
            self._rest_base_url = "https://example.invalid/"
            self.queries: list[str] = []

        def get_saved_query(self, query_id: str) -> dict[str, object]:
            return {"wiql": "Select [System.Id] From WorkItems"}

        def _request_json(self, method: str, url: str, **kwargs: object) -> dict[str, object]:
            wiql = kwargs["json"]["query"]
            self.queries.append(wiql)
            if "[System.Id] > 3" in wiql:
                return terminal
            return {"workItems": [{"id": 1}, {"id": 2}, {"id": 3}]}

    client = Client()
    base = _pagination_config(tmp_path, ["query"])
    result = ADODiscoveryProvider(client).discover(
        "demo", ADODiscoveryConfig(base.slice_contracts, top=3), (),
    )

    assert len(client.queries) == 3
    assert result.completeness is DiscoveryCompleteness.PARTIAL
    assert result.query_results[0].cap_reached is True
    assert result.query_results[0].completeness_state == "PARTIAL"
    assert set(result.query_results[0].membership_ids) == {"1", "2", "3"}


def test_ado_discovery_cap_reached_partitions_to_prove_full_membership(tmp_path: Path) -> None:
    """Contoso spec D-3: a WIQL result at the configured cap must never be
    reported as FULL just because the call itself succeeded (no QueryError) --
    ADODiscoveryProvider must re-scan via a deterministic ID-range cursor and
    only report FULL once that scan proves the complete membership."""
    from src.core.integration_types import ScopeStatusKind

    contract_path = tmp_path / "slice_contracts.yaml"
    contract_path.write_text(
        "\n".join([
            'schema_version: "1.0"',
            "slices:",
            "  - id: demo.capped",
            '    scorecard_name: "Demo"',
            "    section: demo",
            "    workstream: Demo",
            "    slice_kind: scorecard_dimension",
            '    title: "Capped"',
            "    source_of_truth: ado_primary",
            "    owners: {primary: Owner}",
            "    source_contract:",
            "      ado:",
            "        saved_queries: [query-capped]",
            "        tag_expression: {all_of: [ExampleRelease]}",
            "        explicit_work_item_ids: []",
            "        required_fields: [state]",
            "    freshness: {warn_days: 5, block_days: 10}",
            "    degradation: {blank_filter_is_error: true}",
        ]),
        encoding="utf-8",
    )

    class _IdCursorPagingClient(_FakeADOClient):
        """Simulates a real ADO ID-ordered cursor scan: each call returns the
        next `top` ids strictly greater than the `[System.Id] > N` predicate
        embedded in the WIQL (or the first page, unfiltered by id, for the
        initial non-partitioned call)."""

        def __init__(self, all_ids: list[int]) -> None:
            super().__init__()
            self.all_ids = sorted(all_ids)

        def execute_wiql(self, wiql: str, top: int | None = None, *, on_pagination=None) -> list[int]:
            import re

            self.executed_wiql.append(wiql)
            top = top or 0
            match = re.search(r"\[System\.Id\] > (\d+)", wiql)
            cursor = int(match.group(1)) if match else 0
            page = [i for i in self.all_ids if i > cursor][:top]
            if on_pagination is not None:
                from src.core.integration_types import PaginationOutcome

                on_pagination(PaginationOutcome(total_fetched=len(page), page_count=1, is_truncated=len(page) >= top))
            return page

    client = _IdCursorPagingClient(list(range(1, 13)))  # 12 ids, cap 5 -> 3 pages (5, 5, 2)
    provider = ADODiscoveryProvider(client)  # type: ignore[arg-type]
    config = ADODiscoveryConfig(load_slice_contract(contract_path), top=5)

    result = provider.discover("demo", config, ())

    assert result.completeness is DiscoveryCompleteness.FULL
    capped_scope = result.scope_statuses["demo.capped:query-capped"]
    assert capped_scope.status is ScopeStatusKind.SUCCESS
    assert capped_scope.completeness is DiscoveryCompleteness.FULL
    assert capped_scope.item_count == 12
    assert capped_scope.error_message is None
    assert {ref.registration.ref_id for ref in result.discovered_refs} == {str(i) for i in range(1, 13)}
    # First call is the un-partitioned scope query; subsequent calls are the
    # ID-ordered partition scan (3 pages to exhaust 12 ids at cap 5).
    assert len(client.executed_wiql) == 1 + 3


def test_ado_discovery_cap_reached_partition_scan_aborts_as_partial_when_it_cannot_prove_completeness(
    tmp_path: Path,
) -> None:
    """Contoso spec D-3 item 5: if the partition scan cannot make forward
    progress (here: a pathological query whose result doesn't respect the
    `[System.Id] > cursor` predicate at all, so the same ids recur on every
    page) it must abort as PARTIAL rather than loop forever or falsely claim
    completeness."""
    from src.core.integration_types import ScopeStatusKind

    contract_path = tmp_path / "slice_contracts.yaml"
    contract_path.write_text(
        "\n".join([
            'schema_version: "1.0"',
            "slices:",
            "  - id: demo.capped",
            '    scorecard_name: "Demo"',
            "    section: demo",
            "    workstream: Demo",
            "    slice_kind: scorecard_dimension",
            '    title: "Capped"',
            "    source_of_truth: ado_primary",
            "    owners: {primary: Owner}",
            "    source_contract:",
            "      ado:",
            "        saved_queries: [query-capped]",
            "        tag_expression: {all_of: [ExampleRelease]}",
            "        explicit_work_item_ids: []",
            "        required_fields: [state]",
            "    freshness: {warn_days: 5, block_days: 10}",
            "    degradation: {blank_filter_is_error: true}",
        ]),
        encoding="utf-8",
    )

    class _StaticPagingClient(_FakeADOClient):
        """Always returns the same at-cap ids regardless of the id-cursor
        predicate -- an unrealistic but deliberate stand-in for a query the
        ID-range partition scan cannot make progress against."""

        def execute_wiql(self, wiql: str, top: int | None = None, *, on_pagination=None) -> list[int]:
            self.executed_wiql.append(wiql)
            top = top or 0
            page = list(range(1, top + 1))
            if on_pagination is not None:
                from src.core.integration_types import PaginationOutcome

                on_pagination(PaginationOutcome(total_fetched=len(page), page_count=1, is_truncated=len(page) >= top))
            return page

    provider = ADODiscoveryProvider(_StaticPagingClient())  # type: ignore[arg-type]
    config = ADODiscoveryConfig(load_slice_contract(contract_path), top=5)

    result = provider.discover("demo", config, ())

    assert result.completeness is DiscoveryCompleteness.PARTIAL
    capped_scope = result.scope_statuses["demo.capped:query-capped"]
    assert capped_scope.status is ScopeStatusKind.SUCCESS
    assert capped_scope.completeness is DiscoveryCompleteness.PARTIAL
    assert capped_scope.item_count == 5
    assert capped_scope.error_message is not None
    assert "could not prove complete membership" in capped_scope.error_message


def test_ado_discovery_is_idempotent(tmp_path: Path) -> None:
    """Running discovery twice with the same inputs produces identical DiscoveredRef sets."""
    contract_path = tmp_path / "slice_contracts.yaml"
    contract_path.write_text(
        "\n".join([
            'schema_version: "1.0"',
            "slices:",
            "  - id: demo.slice",
            '    scorecard_name: "Demo"',
            "    section: demo",
            "    workstream: Demo",
            "    slice_kind: scorecard_dimension",
            '    title: "Idempotent"',
            "    source_of_truth: ado_primary",
            "    owners: {primary: Owner}",
            "    source_contract:",
            "      ado:",
            "        saved_queries: [query-1]",
            "        tag_expression: {all_of: [ExampleRelease]}",
            "        explicit_work_item_ids: [999]",
            "        required_fields: [state]",
            "    freshness: {warn_days: 5, block_days: 10}",
            "    degradation: {blank_filter_is_error: true}",
        ]),
        encoding="utf-8",
    )
    provider = ADODiscoveryProvider(_FakeADOClient())  # type: ignore[arg-type]
    config = ADODiscoveryConfig(load_slice_contract(contract_path))

    result1 = provider.discover("demo", config, ())
    result2 = provider.discover("demo", config, ())

    assert [ref.registration.ref_id for ref in result1.discovered_refs] == [
        ref.registration.ref_id for ref in result2.discovered_refs
    ]
    assert result1.completeness is result2.completeness


def test_ado_discovery_caches_saved_query_lookup_across_scopes(tmp_path: Path) -> None:
    """The same saved_query id shared by multiple slices with different clauses should only be
    fetched from ADO once per discovery run, not once per (query_id, clause) scope group."""
    contract_path = tmp_path / "slice_contracts.yaml"
    contract_path.write_text(
        "\n".join(
            [
                'schema_version: "1.0"',
                "slices:",
                "  - id: demo.a",
                '    scorecard_name: "Demo"',
                "    section: demo",
                "    workstream: Demo",
                "    slice_kind: scorecard_dimension",
                '    title: "A"',
                "    source_of_truth: ado_primary",
                "    owners: {primary: Owner}",
                "    source_contract:",
                "      ado:",
                "        saved_queries: [query-shared]",
                "        tag_expression: {all_of: [ExampleRelease]}",
                "        explicit_work_item_ids: []",
                "        required_fields: [state]",
                "    freshness: {warn_days: 5, block_days: 10}",
                "    degradation: {blank_filter_is_error: true}",
                "  - id: demo.b",
                '    scorecard_name: "Demo"',
                "    section: demo",
                "    workstream: Demo",
                "    slice_kind: scorecard_dimension",
                '    title: "B"',
                "    source_of_truth: ado_primary",
                "    owners: {primary: Owner}",
                "    source_contract:",
                "      ado:",
                "        saved_queries: [query-shared]",
                "        tag_expression: {all_of: [ExampleFollowup]}",
                "        explicit_work_item_ids: []",
                "        required_fields: [state]",
                "    freshness: {warn_days: 5, block_days: 10}",
                "    degradation: {blank_filter_is_error: true}",
            ]
        ),
        encoding="utf-8",
    )

    class _CountingClient(_FakeADOClient):
        def __init__(self) -> None:
            super().__init__()
            self.get_saved_query_calls: list[str] = []

        def get_saved_query(self, query_id: str) -> dict[str, object]:
            self.get_saved_query_calls.append(query_id)
            return super().get_saved_query(query_id)

    client = _CountingClient()
    provider = ADODiscoveryProvider(client)  # type: ignore[arg-type]

    result = provider.discover("demo", ADODiscoveryConfig(load_slice_contract(contract_path)), ())

    # Two scope groups (different clauses) share query-shared; the saved query definition
    # must only be fetched from ADO once, not twice.
    assert client.get_saved_query_calls == ["query-shared"]
    # Both scopes' WIQL still executed with their own distinct bounded clause.
    assert len(client.executed_wiql) == 2
    assert result.completeness is DiscoveryCompleteness.FULL


def _saved_query_refresh_config(tmp_path: Path) -> ADODiscoveryConfig:
    slices = [
        {
            "id": slice_id, "scorecard_name": "Demo", "section": "demo", "workstream": "Demo",
            "slice_kind": "scorecard_dimension", "title": slice_id, "source_of_truth": "ado_primary",
            "owners": {"primary": "Owner"},
            "source_contract": {"ado": {
                "saved_queries": [query_id], "tag_expression": {"all_of": [tag]},
                "required_fields": ["state"],
            }},
            "freshness": {"warn_days": 5, "block_days": 10},
            "degradation": {"blank_filter_is_error": True},
        }
        for slice_id, query_id, tag in (
            ("demo.a", "shared", "Alpha"),
            ("demo.b", "shared", "Beta"),
            ("demo.independent", "other", "Release"),
        )
    ]
    path = tmp_path / "slice_contracts.yaml"
    path.write_text(yaml.safe_dump({"schema_version": "1.0", "slices": slices}), encoding="utf-8")
    return ADODiscoveryConfig(load_slice_contract(path))


class _MutableSavedQueryClient(_FakeADOClient):
    def __init__(self) -> None:
        super().__init__()
        self.definitions: dict[str, str | None] = {
            "shared": "Select [System.Id] From WorkItems where [System.Id] >= 100",
            "other": "Select [System.Id] From WorkItems where [System.Id] >= 200",
        }
        self.query_calls: list[str] = []
        self.failed_queries: set[str] = set()
        self.ids = {"shared": [101, 102], "other": [201, 202]}

    def get_saved_query(self, query_id: str) -> dict[str, object]:
        from src.core.exceptions import QueryError

        self.query_calls.append(query_id)
        if query_id in self.failed_queries:
            raise QueryError("synthetic private provider detail")
        return {"wiql": self.definitions[query_id]}

    def execute_wiql(self, wiql: str, top: int | None = None, *, on_pagination=None) -> list[int]:
        self.executed_wiql.append(wiql)
        ids = self.ids["other" if "[System.Id] >= 200" in wiql else "shared"]
        if on_pagination is not None:
            on_pagination(PaginationOutcome(len(ids), 1, False))
        return ids


def test_saved_query_refreshes_per_run_and_deduplicates_distinct_clauses(tmp_path: Path) -> None:
    client = _MutableSavedQueryClient()
    provider = ADODiscoveryProvider(client)  # type: ignore[arg-type]
    config = _saved_query_refresh_config(tmp_path)

    first = provider.discover("demo", config, ())
    client.definitions["shared"] = " \nSelect [System.Id] From WorkItems where [System.Id] >= 100\t"
    second = provider.discover("demo", config, ())

    assert first.completeness is second.completeness is DiscoveryCompleteness.FULL
    assert client.query_calls.count("shared") == client.query_calls.count("other") == 2
    assert len(client.executed_wiql) == 6
    assert [(row.scope_id, row.wiql_hash) for row in first.query_results] == [
        (row.scope_id, row.wiql_hash) for row in second.query_results
    ]


def test_saved_query_refresh_pins_an_immutable_definition_within_a_run(tmp_path: Path) -> None:
    class Client(_MutableSavedQueryClient):
        def __init__(self) -> None:
            super().__init__()
            self.payload: dict[str, object] = {"wiql": self.definitions["shared"]}

        def get_saved_query(self, query_id: str) -> dict[str, object]:
            if query_id == "shared":
                self.query_calls.append(query_id)
                return self.payload
            return super().get_saved_query(query_id)

        def execute_wiql(self, wiql: str, top: int | None = None, *, on_pagination=None) -> list[int]:
            self.payload["wiql"] = "Select [System.Id] From WorkItems where [System.Id] >= 150"
            return super().execute_wiql(wiql, top, on_pagination=on_pagination)

    client = Client()
    result = ADODiscoveryProvider(client).discover(  # type: ignore[arg-type]
        "demo", _saved_query_refresh_config(tmp_path), (),
    )

    assert result.completeness is DiscoveryCompleteness.FULL
    assert client.query_calls.count("shared") == 1
    assert sum("[System.Id] >= 100" in wiql for wiql in client.executed_wiql) == 2
    assert all("[System.Id] >= 150" not in wiql for wiql in client.executed_wiql)


@pytest.mark.parametrize("failure", ["changed", "unavailable"])
def test_saved_query_refresh_preserves_failed_scopes_and_independent_retirement(
    tmp_path: Path, failure: str,
) -> None:
    from src.core.channel_registry_store import ChannelRegistryStore

    client = _MutableSavedQueryClient()
    client.ids["other"] = [101, 201, 202]
    provider = ADODiscoveryProvider(client)  # type: ignore[arg-type]
    config = _saved_query_refresh_config(tmp_path)
    previous = provider.discover("demo", config, ())
    store = ChannelRegistryStore(tmp_path / "registry.sqlite", "demo")
    store.apply_discovery_result(previous)

    if failure == "changed":
        client.definitions["shared"] = "Select [System.Id] From WorkItems where [System.Id] >= 150"
    else:
        client.failed_queries.add("shared")
    client.ids["shared"] = [101]
    client.ids["other"] = [101, 201]
    executed_before = len(client.executed_wiql)
    result = provider.discover("demo", config, ())
    delta = store.apply_discovery_result(result)

    assert result.completeness is DiscoveryCompleteness.PARTIAL
    assert {item.ref_id for item in delta.removed} == {"202"}
    assert "101" not in {item.ref_id for item in delta.updated}
    assert {item.ref_id for item in store.active_registrations("ado")} == {"101", "102", "201"}
    overlap = next(ref for ref in store.load_discovered_refs("ado") if ref.registration.ref_id == "101")
    assert {binding.scope_id for binding in overlap.bindings} == {
        "demo.a:shared", "demo.b:shared", "demo.independent:other",
    }
    assert len(client.executed_wiql) == executed_before + 1
    assert result.scope_statuses["demo.independent:other"].completeness is DiscoveryCompleteness.FULL
    for scope_id in ("demo.a:shared", "demo.b:shared"):
        status = result.scope_statuses[scope_id]
        assert status.status is ScopeStatusKind.ERROR
        assert status.completeness is DiscoveryCompleteness.PARTIAL
        assert status.error_message is not None and "private provider detail" not in status.error_message
        row = next(row for row in result.query_results if row.scope_id == scope_id)
        assert row.completeness_state == "ERROR"
        assert row.failure_category == ("saved_query_changed" if failure == "changed" else "query_error")
        assert row.wiql_hash == "" and row.membership_ids == ()

    client.ids["other"] = [201]
    next_partial = provider.discover("demo", config, ())
    next_delta = store.apply_discovery_result(next_partial)
    assert next_delta.removed == ()
    assert {item.ref_id for item in store.active_registrations("ado")} == {"101", "102", "201"}

    client.definitions["shared"] = "Select [System.Id] From WorkItems where [System.Id] >= 100"
    client.failed_queries.clear()
    recovered = provider.discover("demo", config, ())
    assert recovered.completeness is DiscoveryCompleteness.FULL


@pytest.mark.parametrize("previously_valid", [False, True])
def test_saved_query_refresh_recovers_from_missing_wiql(tmp_path: Path, previously_valid: bool) -> None:
    client = _MutableSavedQueryClient()
    provider = ADODiscoveryProvider(client)  # type: ignore[arg-type]
    config = _saved_query_refresh_config(tmp_path)

    if previously_valid:
        assert provider.discover("demo", config, ()).completeness is DiscoveryCompleteness.FULL
    client.definitions["shared"] = None
    first = provider.discover("demo", config, ())
    client.definitions["shared"] = "Select [System.Id] From WorkItems where [System.Id] >= 100"
    second = provider.discover("demo", config, ())

    assert first.completeness is DiscoveryCompleteness.PARTIAL
    assert {row.failure_category for row in first.query_results if row.query_id == "shared"} == {"missing_wiql"}
    assert second.completeness is DiscoveryCompleteness.FULL
    assert client.query_calls.count("shared") == (3 if previously_valid else 2)


def test_saved_query_refresh_observations_are_partitioned_by_program_and_instance(tmp_path: Path) -> None:
    client = _MutableSavedQueryClient()
    provider = ADODiscoveryProvider(client)  # type: ignore[arg-type]
    config = _saved_query_refresh_config(tmp_path)
    assert provider.discover("demo", config, ()).completeness is DiscoveryCompleteness.FULL

    client.definitions["shared"] = "Select [System.Id] From WorkItems where [System.Id] >= 150"
    other_instance = ADODiscoveryConfig(config.slice_contracts, provider_instance_id="other")
    assert provider.discover("other-program", config, ()).completeness is DiscoveryCompleteness.FULL
    assert provider.discover("demo", other_instance, ()).completeness is DiscoveryCompleteness.FULL
    assert provider.discover("demo", config, ()).completeness is DiscoveryCompleteness.PARTIAL


def test_saved_query_refresh_is_synchronized_for_concurrent_discovery(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, Lock

    class Client(_MutableSavedQueryClient):
        def __init__(self) -> None:
            super().__init__()
            self.barrier = Barrier(2)
            self.lock = Lock()
            self.shared_calls = 0

        def get_saved_query(self, query_id: str) -> dict[str, object]:
            if query_id != "shared":
                return super().get_saved_query(query_id)
            with self.lock:
                self.shared_calls += 1
                bound = 100 if self.shared_calls == 1 else 150
            self.barrier.wait(timeout=10)
            return {"wiql": f"Select [System.Id] From WorkItems where [System.Id] >= {bound}"}

    client = Client()
    provider = ADODiscoveryProvider(client)  # type: ignore[arg-type]
    config = _saved_query_refresh_config(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(provider.discover, "demo", config, ()) for _ in range(2)]
        results = [future.result(timeout=15) for future in futures]

    assert {result.completeness for result in results} == {
        DiscoveryCompleteness.FULL, DiscoveryCompleteness.PARTIAL,
    }
    partial = next(result for result in results if result.completeness is DiscoveryCompleteness.PARTIAL)
    assert {row.failure_category for row in partial.query_results if row.query_id == "shared"} == {
        "saved_query_changed",
    }
    assert len([wiql for wiql in client.executed_wiql if "[System.Id] >= 200" not in wiql]) == 2


def test_ado_discovery_no_ado_contract_slices_returns_empty(tmp_path: Path) -> None:
    """Slices with no ADO source contract produce zero discovered refs (workstream_ids=() edge case)."""
    contract_path = tmp_path / "slice_contracts.yaml"
    contract_path.write_text(
        "\n".join([
            'schema_version: "1.0"',
            "slices:",
            "  - id: demo.no_ado",
            '    scorecard_name: "Demo"',
            "    section: demo",
            "    workstream: Demo",
            "    slice_kind: scorecard_dimension",
            '    title: "No ADO"',
            "    source_of_truth: ado_primary",
            "    owners: {primary: Owner}",
            "    source_contract: {}",
            "    freshness: {warn_days: 5, block_days: 10}",
            "    degradation: {blank_filter_is_error: true}",
        ]),
        encoding="utf-8",
    )
    provider = ADODiscoveryProvider(_FakeADOClient())  # type: ignore[arg-type]

    result = provider.discover("demo", ADODiscoveryConfig(load_slice_contract(contract_path)), ())

    assert result.discovered_refs == ()
    assert result.completeness is DiscoveryCompleteness.FULL


@pytest.fixture
def synthetic_binding_contracts(tmp_path: Path) -> tuple[SliceContract, ...]:
    """Exercise binding semantics without opening private program configuration."""
    current_query = CURRENT_QUERY
    history_query = HISTORY_QUERY
    area_path = "ExampleProject\\Services\\Catalog"
    slices = []
    for slice_id, bindings in [
        ("service_checks", [
            {"binding_id": "catalog-current", "query_id": "synthetic-catalog", "mode": "full_scope",
             "filter": {"all_of": [{"field": "area_path", "op": "under", "value": area_path}]}},
            {"binding_id": "worker-current", "query_id": current_query, "mode": "full_scope",
             "required": True, "lane_ids": ["worker"],
             "filter": {"all_of": [{"field": "work_item_type", "op": "eq", "value": "Feature"}]}},
            {"binding_id": "overall-open-validation", "query_id": current_query,
             "mode": "full_scope", "required": False},
            {"binding_id": "overall-all-states-audit", "query_id": history_query,
             "mode": "analytics_history", "required": False},
        ]),
        ("provisioning", [
            {"binding_id": "provisioning-current", "query_id": "synthetic-provisioning", "mode": "full_scope",
             "filter": {"all_of": [{"field": "area_path", "op": "not_under", "value": area_path}]}},
        ]),
    ]:
        slices.append({
            "id": f"example_weekly.{slice_id}", "scorecard_name": "Synthetic",
            "section": slice_id, "workstream": slice_id, "slice_kind": "scorecard_dimension",
            "title": slice_id, "source_of_truth": "ado_primary", "owners": {"primary": "Owner"},
            "source_contract": {"ado": {"saved_queries": bindings}},
            "freshness": {"warn_days": 5, "block_days": 10},
            "degradation": {"blank_filter_is_error": True},
        })
    path = tmp_path / "slice_contracts.yaml"
    path.write_text(yaml.safe_dump({"schema_version": "1.1", "slices": slices}), encoding="utf-8")
    return load_slice_contract(path)


def test_binding_discovery_scopes_honor_binding_filters_and_exclude_history(
    synthetic_binding_contracts: tuple[SliceContract, ...],
) -> None:
    contracts = synthetic_binding_contracts

    scopes = _discovery_scopes(contracts)
    by_scope_id = {scope.scope_id: scope for scope in scopes}

    catalog = by_scope_id["example_weekly.service_checks:catalog-current"]
    provisioning = by_scope_id["example_weekly.provisioning:provisioning-current"]
    worker = by_scope_id["example_weekly.service_checks:worker-current"]

    assert "[System.AreaPath] under 'ExampleProject\\Services\\Catalog'" in catalog.clause
    assert "[System.AreaPath] not under 'ExampleProject\\Services\\Catalog'" in provisioning.clause
    assert "[System.WorkItemType] = 'Feature'" in worker.clause
    assert all(scope.query_id != HISTORY_QUERY for scope in scopes)


def test_binding_overall_queries_have_distinct_governed_roles(
    synthetic_binding_contracts: tuple[SliceContract, ...],
) -> None:
    """Validation and history bindings must not silently become broad scope."""
    contracts = synthetic_binding_contracts
    bindings = {
        binding.binding_id: binding
        for contract in contracts
        for binding in (
            contract.source_contract.ado.saved_query_bindings
            if contract.source_contract.ado is not None
            else ()
        )
    }

    worker = bindings["worker-current"]
    validation = bindings["overall-open-validation"]
    all_states_audit = bindings["overall-all-states-audit"]

    assert worker.query_id == validation.query_id == CURRENT_QUERY
    assert worker.mode == "full_scope"
    assert worker.required is True
    assert worker.lane_ids == ("worker",)
    assert validation.mode == "full_scope"
    assert validation.required is False
    assert validation.lane_ids == ()
    assert all_states_audit.query_id == HISTORY_QUERY
    assert all_states_audit.mode == "analytics_history"
    assert all_states_audit.required is False


# ---------------------------------------------------------------------------
# RELO-W2.2: expand_with_linked_items had zero direct unit coverage before
# this -- only ever exercised through a fake lambda in the wiql-stage
# caller's tests, never its real implementation.
# ---------------------------------------------------------------------------


def _relation(rel: str, url: str) -> dict[str, object]:
    return {"rel": rel, "url": url}


class _RelationsClient:
    def __init__(self, relations_by_seed: dict[int, list[dict[str, object]]]) -> None:
        self._relations_by_seed = relations_by_seed

    def get_work_item_relations(self, work_item_ids: list[int]) -> list[dict[str, object]]:
        return [
            {"id": work_item_id, "relations": self._relations_by_seed.get(work_item_id, [])}
            for work_item_id in work_item_ids
        ]


def test_expand_with_linked_items_returns_typed_result_with_linked_work_items() -> None:
    client = _RelationsClient({
        1001: [_relation("System.LinkTypes.Hierarchy-Forward", "vstfs:///WorkItemTracking/WorkItem/2001")],
    })
    result = expand_with_linked_items(client, frozenset({1001}))
    assert result == LinkedItemExpansionResult(linked_ids=frozenset({2001}), external_target_count=0)


def test_expand_with_linked_items_counts_external_target_instead_of_silently_dropping() -> None:
    # RELO-W2.2 (Sec 8.3.2): "external target fixture resolved" -- a
    # Related-typed relation pointing at a non-work-item URL must be
    # counted, not vanish with zero trace.
    client = _RelationsClient({
        1001: [_relation("System.LinkTypes.Related", "vstfs:///Git/Commit/abc123")],
    })
    result = expand_with_linked_items(client, frozenset({1001}))
    assert result.linked_ids == frozenset()
    assert result.external_target_count == 1


def test_expand_with_linked_items_mixed_work_item_and_external_targets() -> None:
    client = _RelationsClient({
        1001: [
            _relation("System.LinkTypes.Dependency-Forward", "vstfs:///WorkItemTracking/WorkItem/2001"),
            _relation("System.LinkTypes.Related", "https://example.com/external-doc"),
        ],
    })
    result = expand_with_linked_items(client, frozenset({1001}))
    assert result.linked_ids == frozenset({2001})
    assert result.external_target_count == 1


def test_expand_with_linked_items_ignores_unrelated_relation_types() -> None:
    client = _RelationsClient({
        1001: [_relation("System.LinkTypes.ArtifactLink", "vstfs:///Git/Commit/abc123")],
    })
    result = expand_with_linked_items(client, frozenset({1001}))
    assert result == LinkedItemExpansionResult(linked_ids=frozenset(), external_target_count=0)


def test_expand_with_linked_items_excludes_seed_ids_from_discovered() -> None:
    client = _RelationsClient({
        1001: [_relation("System.LinkTypes.Related", "vstfs:///WorkItemTracking/WorkItem/1001")],
    })
    result = expand_with_linked_items(client, frozenset({1001}))
    assert result.linked_ids == frozenset()


def test_expand_with_linked_items_empty_seed_returns_empty_result() -> None:
    result = expand_with_linked_items(_RelationsClient({}), frozenset())
    assert result == LinkedItemExpansionResult(linked_ids=frozenset(), external_target_count=0)


def test_expand_with_linked_items_provider_exception_degrades_to_partial_result() -> None:
    class _RaisingClient:
        def get_work_item_relations(self, work_item_ids: list[int]) -> list[dict[str, object]]:
            raise RuntimeError("transport failed")

    result = expand_with_linked_items(_RaisingClient(), frozenset({1001}))
    assert result == LinkedItemExpansionResult(linked_ids=frozenset(), external_target_count=0)


@pytest.mark.parametrize("rel_type", [
    "System.LinkTypes.Duplicate-Forward",
    "System.LinkTypes.Affects-Reverse",
    "Custom.Dependency-Forward",
    "CustomDependencySuccessor",
])
def test_expansion_quarantines_unknown_types_visibly(rel_type: str) -> None:
    client = _RelationsClient({
        1001: [_relation(rel_type, "vstfs:///WorkItemTracking/WorkItem/2001")],
        2001: [_relation("Related", "vstfs:///WorkItemTracking/WorkItem/3001")],
    })
    result = expand_with_linked_items(client, frozenset({1001}), max_depth=2)
    assert result.linked_ids == frozenset()
    assert result.external_target_count == 0
    assert len(result.errors) == 1
    error = result.errors[0]
    assert error.ref_id == "1001"
    assert rel_type in error.message
    assert "2001" in error.message
    assert "quarantined" in error.message
    assert error.retryable is False
    assert error.operator_action


@pytest.mark.parametrize("rel_type", [
    "System.LinkTypes.Hierarchy-Forward", "Hierarchy-Forward",
    "System.LinkTypes.Related", "Related",
    "System.LinkTypes.Dependency-Forward", "Dependency-Forward",
    "System.LinkTypes.Dependency-Reverse", "Dependency-Reverse",
    "System.LinkTypes.DependencyPredecessor", "DependencyPredecessor",
    "System.LinkTypes.DependencySuccessor", "DependencySuccessor",
])
def test_expansion_uses_recognized_work_item_relations(rel_type: str) -> None:
    client = _RelationsClient({
        1001: [_relation(rel_type, "https://dev.azure.com/demo/project/_apis/wit/workItems/2001")],
    })
    result = expand_with_linked_items(client, frozenset({1001}))
    assert result.linked_ids == frozenset({2001})
    assert result.errors == ()


@pytest.mark.parametrize("url", [
    "https://example.com/pull/2001",
    "vstfs:///Git/Commit/2001",
    "https://example.com/WorkItemTracking/WorkItem/2001",
])
def test_expansion_does_not_follow_numeric_non_work_item_targets(url: str) -> None:
    result = expand_with_linked_items(
        _RelationsClient({1001: [_relation("System.LinkTypes.Related", url)]}), frozenset({1001}),
    )
    assert result.linked_ids == frozenset()
    assert result.external_target_count == 1


@pytest.mark.parametrize("rel_type", [
    "System.LinkTypes.Hierarchy-Reverse",
    "System.LinkTypes.ArtifactLink",
    "System.LinkTypes.ExternalLink",
])
def test_expansion_does_not_broaden_to_known_unsupported_links(rel_type: str) -> None:
    result = expand_with_linked_items(
        _RelationsClient({1001: [_relation(rel_type, "vstfs:///WorkItemTracking/WorkItem/2001")]}),
        frozenset({1001}),
    )
    assert result.linked_ids == frozenset()
    assert result.external_target_count == 0
    assert result.errors == ()


def test_expansion_preserves_known_links_and_quarantine_across_depths() -> None:
    client = _RelationsClient({
        1001: [
            _relation("System.LinkTypes.Affects-Forward", "vstfs:///WorkItemTracking/WorkItem/9001"),
            _relation("System.LinkTypes.Related", "vstfs:///WorkItemTracking/WorkItem/2001"),
        ],
        2001: [
            _relation("System.LinkTypes.Duplicate-Reverse", "vstfs:///WorkItemTracking/WorkItem/9002"),
            _relation("System.LinkTypes.Dependency-Reverse", "vstfs:///WorkItemTracking/WorkItem/3001"),
        ],
        3001: [_relation("Related", "vstfs:///WorkItemTracking/WorkItem/4001")],
        9001: [_relation("Related", "vstfs:///WorkItemTracking/WorkItem/9003")],
    })
    result = expand_with_linked_items(client, frozenset({1001}), max_depth=2)
    assert result.linked_ids == frozenset({2001, 3001})
    assert {error.ref_id for error in result.errors} == {"1001", "2001"}
    assert len(result.errors) == 2
