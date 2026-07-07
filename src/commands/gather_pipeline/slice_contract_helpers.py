from __future__ import annotations

from typing import Any

from src.core.slice_contract_loader import SliceContract


def slice_contract_saved_query_clauses(slice_contracts: tuple[SliceContract, ...] | None) -> dict[str, str]:
    if not slice_contracts:
        return {}

    clauses_by_query_id: dict[str, list[str]] = {}
    for contract in slice_contracts:
        ado_contract = contract.source_contract.ado
        if ado_contract is None:
            continue
        clause_parts = [
            clause
            for clause in (
                render_saved_query_filter_clause(ado_contract.filters) if ado_contract.filters is not None else "",
                render_tag_expression_clause(ado_contract.tag_expression),
            )
            if clause
        ]
        clause = " and ".join(f"({part})" for part in clause_parts)
        if not clause:
            continue
        for query_id in ado_contract.saved_queries:
            clauses_by_query_id.setdefault(query_id, []).append(clause)

    merged_clauses: dict[str, str] = {}
    for query_id, clauses in clauses_by_query_id.items():
        ordered_unique_clauses = tuple(dict.fromkeys(clause for clause in clauses if clause))
        if not ordered_unique_clauses:
            continue
        if len(ordered_unique_clauses) == 1:
            merged_clauses[query_id] = ordered_unique_clauses[0]
            continue
        merged_clauses[query_id] = "(" + " or ".join(ordered_unique_clauses) + ")"
    return merged_clauses


def render_saved_query_filter_clause(filter_definition: Any) -> str:
    def _render_predicate(predicate: Any) -> str | None:
        field_name = str(getattr(predicate, "field", "")).strip().lower()
        operator = str(getattr(predicate, "op", "")).strip().lower()
        raw_value = str(getattr(predicate, "value", "")).strip()
        if not raw_value:
            return None

        field_ref = {
            "title": "[System.Title]",
            "tag": "[System.Tags]",
            "area_path": "[System.AreaPath]",
        }.get(field_name)
        if field_ref is None:
            return None

        escaped_value = raw_value.replace("'", "''")
        if field_name == "area_path":
            if operator == "eq":
                return f"{field_ref} = '{escaped_value}'"
            if operator == "contains" and "\\" in raw_value:
                return f"{field_ref} under '{escaped_value}'"
            return None
        if operator == "contains":
            return f"{field_ref} contains '{escaped_value}'"
        if operator == "eq":
            if field_name == "tag":
                return f"{field_ref} Contains Words '{escaped_value}'"
            return f"{field_ref} = '{escaped_value}'"
        return None

    all_of_parts = [
        rendered
        for rendered in (_render_predicate(predicate) for predicate in getattr(filter_definition, "all_of", ()))
        if rendered is not None
    ]
    any_of_parts = [
        rendered
        for rendered in (_render_predicate(predicate) for predicate in getattr(filter_definition, "any_of", ()))
        if rendered is not None
    ]

    if any_of_parts:
        groups = []
        for rendered_any_of in any_of_parts:
            group_parts = [*all_of_parts, rendered_any_of]
            groups.append("(" + " and ".join(group_parts) + ")")
        return " or ".join(groups)
    if all_of_parts:
        return "(" + " and ".join(all_of_parts) + ")"
    return ""


def render_tag_expression_clause(tag_expression: Any) -> str:
    if tag_expression is None:
        return ""

    def _render_tag(tag: str) -> str | None:
        value = str(tag).strip()
        if not value:
            return None
        escaped_value = value.replace("'", "''")
        return f"[System.Tags] Contains Words '{escaped_value}'"

    all_of_parts = [part for part in (_render_tag(tag) for tag in getattr(tag_expression, "all_of", ())) if part]
    any_of_parts = [part for part in (_render_tag(tag) for tag in getattr(tag_expression, "any_of", ())) if part]
    if not all_of_parts and not any_of_parts:
        return ""
    if any_of_parts:
        return " and ".join([*all_of_parts, "(" + " or ".join(any_of_parts) + ")"])
    return " and ".join(all_of_parts)
