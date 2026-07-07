"""Direct coverage for extracted gather slice-contract clause helpers."""

from __future__ import annotations

from pathlib import Path

from src.commands.gather_pipeline import slice_contract_helpers
from src.commands.gather import load_slice_contract


def test_slice_contract_saved_query_clauses_apply_tag_expression(tmp_path: Path) -> None:
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
                "          any_of: [Contoso, Acme]",
                "        explicit_work_item_ids: []",
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

    contracts = load_slice_contract(contract_path)

    assert slice_contract_helpers.slice_contract_saved_query_clauses(contracts) == {
        "query-1": "([System.Tags] Contains Words 'RAMPP1' and ([System.Tags] Contains Words 'Contoso' or [System.Tags] Contains Words 'Acme'))"
    }


def test_render_saved_query_filter_clause_uses_contains_words_for_tag_eq() -> None:
    class _Predicate:
        def __init__(self, field: str, op: str, value: str) -> None:
            self.field = field
            self.op = op
            self.value = value

    class _Filter:
        all_of = (_Predicate("tag", "eq", "Acme"),)
        any_of = ()

    assert (
        slice_contract_helpers.render_saved_query_filter_clause(_Filter())
        == "([System.Tags] Contains Words 'Acme')"
    )
