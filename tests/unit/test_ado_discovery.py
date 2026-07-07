from __future__ import annotations

from pathlib import Path

from src.core.ado_discovery import ADODiscoveryConfig, ADODiscoveryProvider
from src.core.integration_types import DiscoveryCompleteness
from src.core.slice_contract_loader import load_slice_contract


class _FakeADOClient:
    def __init__(self) -> None:
        self.executed_wiql: list[str] = []

    def get_saved_query(self, query_id: str) -> dict[str, object]:
        return {"id": query_id, "wiql": "Select [System.Id] From WorkItems"}

    def execute_wiql(self, wiql: str, top: int | None = None) -> list[int]:
        self.executed_wiql.append(wiql)
        assert top == 10000
        return [101, 102]


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
