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
    expand_with_linked_items,
)
from src.core.integration_types import DiscoveryCompleteness
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
