from __future__ import annotations

from pathlib import Path

import pytest

from src.core.ado_discovery import ADODiscoveryConfig, ADODiscoveryProvider, _discovery_scopes
from src.core.integration_types import DiscoveryCompleteness
from src.core.slice_contract_loader import load_slice_contract


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
                "          all_of: [RAMPP1]",
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
        "Select [System.Id] From WorkItems where (([System.Tags] Contains Words 'RAMPP1'))"
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
                "        tag_expression: {all_of: [RAMPP1]}",
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
                "        tag_expression: {all_of: [RAMPP1]}",
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
            "        tag_expression: {all_of: [RAMPP1]}",
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
            "        tag_expression: {all_of: [RAMPP1]}",
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
    """Armada spec D-3: a WIQL result at the configured cap must never be
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
            "        tag_expression: {all_of: [RAMPP1]}",
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
    """Armada spec D-3 item 5: if the partition scan cannot make forward
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
            "        tag_expression: {all_of: [RAMPP1]}",
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
            "        tag_expression: {all_of: [RAMPP1]}",
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
                "        tag_expression: {all_of: [RAMPP1]}",
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
                "        tag_expression: {all_of: [RAMPP2]}",
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


_ARMADA_SLICE_CONTRACTS_MISSING = not Path("programs/armada/slice_contracts.yaml").exists()
_ARMADA_SKIP_REASON = "programs/armada/ is real, gitignored program data -- not present on a fresh clone/CI"


@pytest.mark.skipif(_ARMADA_SLICE_CONTRACTS_MISSING, reason=_ARMADA_SKIP_REASON)
def test_armada_discovery_scopes_honor_binding_filters_and_exclude_history() -> None:
    contracts = load_slice_contract(Path("programs/armada/slice_contracts.yaml"))

    scopes = _discovery_scopes(contracts)
    by_scope_id = {scope.scope_id: scope for scope in scopes}

    xcatalog = by_scope_id["armada_weekly_update.armada_core_runtime_platform_topology:xcatalog-current"]
    buildout = by_scope_id["armada_weekly_update.buildouts:buildout-current"]
    xcompute = by_scope_id["armada_weekly_update.armada_core_runtime_platform_topology:xcompute-current"]

    assert "[System.AreaPath] under 'One\\Xstore\\XHealth\\Buildout-Romania\\XCatalog'" in xcatalog.clause
    assert "[System.AreaPath] not under 'One\\Xstore\\XHealth\\Buildout-Romania\\XCatalog'" in buildout.clause
    assert "[System.WorkItemType] = 'Feature'" in xcompute.clause
    assert all(scope.query_id != "c6abfbc6-8d20-4393-9782-f9e3608940f9" for scope in scopes)


@pytest.mark.skipif(_ARMADA_SLICE_CONTRACTS_MISSING, reason=_ARMADA_SKIP_REASON)
def test_armada_overall_queries_have_distinct_governed_roles() -> None:
    """ARM-GATHER-1: neither Overall query may silently become broad scope."""
    contracts = load_slice_contract(Path("programs/armada/slice_contracts.yaml"))
    bindings = {
        binding.binding_id: binding
        for contract in contracts
        for binding in (
            contract.source_contract.ado.saved_query_bindings
            if contract.source_contract.ado is not None
            else ()
        )
    }

    xcompute = bindings["xcompute-current"]
    validation = bindings["overall-open-validation"]
    all_states_audit = bindings["overall-all-states-audit"]

    assert xcompute.query_id == validation.query_id == "bdad4a15-8cfe-44ef-bc07-396941754f5f"
    assert xcompute.mode == "full_scope"
    assert xcompute.required is True
    assert xcompute.lane_ids == ("xcompute",)
    assert validation.mode == "full_scope"
    assert validation.required is False
    assert validation.lane_ids == ()
    assert all_states_audit.query_id == "c6abfbc6-8d20-4393-9782-f9e3608940f9"
    assert all_states_audit.mode == "analytics_history"
    assert all_states_audit.required is False
