from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
import hashlib
import math
from pathlib import Path
from typing import Any

from src.core.exceptions import ConfigError, QueryError
from src.core.kusto_client import KustoClient, KustoColumn
from src.core.metric_models import MetricDefinition, MetricSourceBinding
from src.core.metric_registry import _coerce_metric_entries, _parse_metric_definition


_NUMERIC_KUSTO_TYPES = {"int", "long", "real", "decimal", "double", "float"}
MetricBindingProbe = Callable[[MetricSourceBinding], tuple[list[dict[str, Any]], tuple[KustoColumn, ...]]]


def kusto_scalar_cardinality_error(
    query_id: str, expected_cardinality: str, row_count: int, *, metric_id: str | None = None,
) -> str | None:
    if expected_cardinality == "scalar_required" and row_count != 1:
        return (
            f"Kusto query {query_id} expected scalar_required (exactly 1 row); "
            f"actual {row_count} row(s)."
        )
    if metric_id is not None and row_count > 1:
        return (
            f"Kusto query {query_id} returned {row_count} row(s) "
            f"for metric {metric_id}; scalar interpretation is ambiguous "
            "without a deterministic selection contract."
        )
    return None


def kusto_metric_result_error(
    query_id: str, expected_cardinality: str, rows: Sequence[Mapping[str, Any]],
    *, metric_id: str | None = None, result_column: str | None = None,
) -> str | None:
    error = kusto_scalar_cardinality_error(
        query_id, expected_cardinality, len(rows), metric_id=metric_id,
    )
    if error is not None or metric_id is None or not rows:
        return error
    value = rows[0].get(result_column) if result_column else None
    if coerce_metric_value(value) is None:
        return (
            f"Kusto query {query_id} requires a finite numeric value in its declared "
            f"result column for metric {metric_id}."
        )
    return None


def coerce_metric_value(value: Any) -> float | None:
    if value is None or isinstance(value, (bool, dict)):
        return None
    normalized = str(value).replace(",", "").rstrip("%")
    try:
        number = float(normalized)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


@dataclass(frozen=True, slots=True)
class MetricBindingValidation:
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    metric_count: int
    query_count: int
    binding_count: int
    scope: str = "offline_metric_query_bindings"


def validate_metric_binding_documents(
    *,
    program_id: str,
    metric_documents: Mapping[Path, object],
    query_documents: Mapping[Path, object],
) -> MetricBindingValidation:
    """Check supplied authored documents only; no file, state or provider access."""
    errors: list[str] = []
    warnings = [
        "Offline authored metric/query binding checks only: live result schema, numeric types, "
        "dimensions, cardinality, freshness and persisted database bindings were not checked. "
        "Schema versions, review currency and full metric interpretation are outside this scope."
    ]
    definitions: dict[str, MetricDefinition] = {}
    parsed_definitions: list[MetricDefinition] = []
    for path, document in metric_documents.items():
        try:
            if isinstance(document, dict) and "metrics" not in document:
                raise ConfigError(f"Metric registry must contain metrics: list in {path}")
            if document is None:
                raise ConfigError(f"Metric registry document is empty in {path}")
            entries = _coerce_metric_entries(document, path)
        except ConfigError as error:
            errors.append(str(error))
            continue
        for entry in entries:
            try:
                definition = _parse_metric_definition(entry, path)
            except (ConfigError, ValueError) as error:
                errors.append(f"{path}: {error}")
                continue
            parsed_definitions.append(definition)
            definitions[definition.id] = definition
    for definition in parsed_definitions:
        for comparable_id in definition.comparable_with:
            if comparable_id not in definitions:
                errors.append(
                    f"Metric {definition.id} comparable_with references missing metric {comparable_id}."
                )

    query_ids: set[str] = set()
    binding_count = 0
    for path, document in query_documents.items():
        collection = {"kpis.yaml": "kpis", "golden_queries.yaml": "queries"}.get(path.name)
        if collection is None:
            errors.append(f"Unsupported query document {path}; expected kpis.yaml or golden_queries.yaml.")
            continue
        if not isinstance(document, dict) or not isinstance(document.get(collection), list):
            errors.append(f"Query document must contain {collection}: list in {path}")
            continue
        for index, entry in enumerate(document[collection]):
            location = f"{path} {collection}[{index}]"
            try:
                if not isinstance(entry, dict):
                    raise ConfigError(f"Query entry must be an object in {location}")
                program_ids = entry.get("program_ids", [])
                if not isinstance(program_ids, list) or any(
                    not isinstance(value, str) or not value.strip() for value in program_ids
                ):
                    raise ConfigError(f"program_ids must be a list of non-empty strings in {location}")
                if program_ids and program_id not in program_ids:
                    continue
                query_id = _binding_document_text(entry, "id", location)
                if query_id is None:
                    raise ConfigError(f"Query id must be a non-empty string in {location}")
                if query_id in query_ids:
                    raise ConfigError(f"Duplicate query id '{query_id}' in {location}")
                query_ids.add(query_id)
                metric_id = _binding_document_text(entry, "metric_id", location)
                engine = _binding_document_text(entry, "engine", location)
                fields = {
                    name: _binding_document_text(entry, name, location)
                    for name in ("cluster", "database", "kql", "wiql", "kql_file", "result_column")
                }
                if metric_id is None:
                    continue
                if metric_id not in definitions:
                    raise ConfigError(f"Metric definition not found for query {query_id} metric {metric_id} in {path}.")
                engine = engine or ("wiql" if collection == "queries" and fields["wiql"] else "kusto")
                if engine not in {"kusto", "wiql"}:
                    warnings.append(f"Query {query_id} engine '{engine}' is unsupported; binding was not checked.")
                    continue
                if engine == "wiql" and fields["wiql"] is None:
                    raise ConfigError(f"Query {query_id} is missing wiql.")
                file_backed = engine == "kusto" and fields["kql"] is None and fields["kql_file"] is not None
                binding = MetricSourceBinding(
                    binding_id=query_id,
                    metric_id=metric_id,
                    program_id=program_id,
                    source_kind="wiql" if engine == "wiql" else "kusto",
                    cluster=fields["cluster"],
                    database=fields["database"],
                    kql_template=fields["wiql"] if engine == "wiql" else fields["kql"],
                    result_column=fields["result_column"],
                )
                _validate_metric_source_binding_fields(
                    binding, metric_definitions=definitions, require_inline_query=not file_backed,
                )
                if file_backed:
                    warnings.append(f"Query {query_id} kql_file was not read; query text/binding was not checked.")
                    continue
                binding_count += 1
            except ConfigError as error:
                errors.append(f"{location}: {error}")
    return MetricBindingValidation(
        errors=tuple(errors),
        warnings=tuple(warnings),
        metric_count=len(definitions),
        query_count=len(query_ids),
        binding_count=binding_count,
    )


def _binding_document_text(entry: dict[str, Any], field: str, location: str) -> str | None:
    value = entry.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"Query field '{field}' must be a string in {location}")
    return value.strip() or None


def validate_metric_source_binding(
    binding: MetricSourceBinding,
    *,
    metric_definitions: dict[str, MetricDefinition],
    probe: MetricBindingProbe,
    validated_at: datetime,
) -> MetricSourceBinding:
    definition = _validate_metric_source_binding_fields(binding, metric_definitions=metric_definitions)

    rows, schema = probe(binding)
    if not schema_contains_column(schema, binding.result_column or ""):
        raise QueryError(
            f"result_column '{binding.result_column}' was not returned for binding {binding.binding_id}."
        )
    if not column_is_numeric(schema, binding.result_column or "", rows):
        raise QueryError(
            f"result_column '{binding.result_column}' is not numeric for binding {binding.binding_id}."
        )
    missing_dimensions = [
        column for column in definition.dimension_columns if not schema_contains_column(schema, column)
    ]
    if missing_dimensions:
        raise QueryError(
            f"Binding {binding.binding_id} is missing dimension column(s): {', '.join(missing_dimensions)}."
        )

    return replace(
        binding,
        validated=True,
        last_validated_at=validated_at,
        last_validated_kql_hash=compute_metric_binding_validation_hash(binding),
    )


def _validate_metric_source_binding_fields(
    binding: MetricSourceBinding,
    *,
    metric_definitions: dict[str, MetricDefinition],
    require_inline_query: bool = True,
) -> MetricDefinition:
    definition = metric_definitions.get(binding.metric_id)
    if definition is None:
        raise ConfigError(f"Metric definition not found for binding metric {binding.metric_id}.")
    if binding.source_kind == "kusto" and (binding.cluster is None or not binding.cluster.strip()):
        raise ConfigError(f"Binding {binding.binding_id} is missing cluster.")
    if binding.source_kind == "kusto" and (binding.database is None or not binding.database.strip()):
        raise ConfigError(f"Binding {binding.binding_id} is missing database.")
    if require_inline_query and (binding.kql_template is None or not binding.kql_template.strip()):
        raise ConfigError(f"Binding {binding.binding_id} is missing kql_template.")
    if binding.result_column is None or not binding.result_column.strip():
        raise ConfigError(f"Binding {binding.binding_id} is missing result_column.")

    return definition


def build_live_metric_binding_probe() -> MetricBindingProbe:
    client = KustoClient()

    def probe(binding: MetricSourceBinding) -> tuple[list[dict[str, Any]], tuple[KustoColumn, ...]]:
        return client.execute_with_schema(
            binding.cluster or "",
            binding.database or "",
            build_metric_validation_kql(binding.kql_template or ""),
        )

    return probe


def build_metric_validation_kql(kql: str) -> str:
    normalized = kql.strip()
    if normalized.endswith(";"):
        normalized = normalized[:-1].rstrip()
    return f"{normalized}\n| take 1"


def compute_metric_validation_kql_hash(rendered_kql: str) -> str:
    return hashlib.sha256(rendered_kql.encode("utf-8")).hexdigest()


def compute_metric_binding_validation_hash(binding: MetricSourceBinding) -> str:
    template = (binding.kql_template or "").strip()
    if binding.source_kind == "kusto":
        return compute_metric_validation_kql_hash(build_metric_validation_kql(template))
    return hashlib.sha256(template.encode("utf-8")).hexdigest()


def schema_contains_column(schema: tuple[KustoColumn, ...], column_name: str) -> bool:
    normalized = column_name.strip().lower()
    return any(column.name.strip().lower() == normalized for column in schema)


def column_is_numeric(
    schema: tuple[KustoColumn, ...],
    column_name: str,
    rows: list[dict[str, Any]],
) -> bool:
    normalized = column_name.strip().lower()
    for column in schema:
        if column.name.strip().lower() != normalized:
            continue
        if column.type_name is not None and str(column.type_name).strip().lower() in _NUMERIC_KUSTO_TYPES:
            return True
        break
    for row in rows:
        for key, value in row.items():
            if key.strip().lower() != normalized:
                continue
            if isinstance(value, bool):
                return False
            if isinstance(value, (int, float)):
                return True
    return False