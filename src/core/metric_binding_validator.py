from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
import hashlib
from typing import Any

from src.core.exceptions import ConfigError, QueryError
from src.core.kusto_client import KustoClient, KustoColumn
from src.core.metric_models import MetricDefinition, MetricSourceBinding


_NUMERIC_KUSTO_TYPES = {"int", "long", "real", "decimal", "double", "float"}
MetricBindingProbe = Callable[[MetricSourceBinding], tuple[list[dict[str, Any]], tuple[KustoColumn, ...]]]


def validate_metric_source_binding(
    binding: MetricSourceBinding,
    *,
    metric_definitions: dict[str, MetricDefinition],
    probe: MetricBindingProbe,
    validated_at: datetime,
) -> MetricSourceBinding:
    definition = metric_definitions.get(binding.metric_id)
    if definition is None:
        raise ConfigError(f"Metric definition not found for binding metric {binding.metric_id}.")
    if binding.source_kind == "kusto" and (binding.cluster is None or not binding.cluster.strip()):
        raise ConfigError(f"Binding {binding.binding_id} is missing cluster.")
    if binding.source_kind == "kusto" and (binding.database is None or not binding.database.strip()):
        raise ConfigError(f"Binding {binding.binding_id} is missing database.")
    if binding.kql_template is None or not binding.kql_template.strip():
        raise ConfigError(f"Binding {binding.binding_id} is missing kql_template.")
    if binding.result_column is None or not binding.result_column.strip():
        raise ConfigError(f"Binding {binding.binding_id} is missing result_column.")

    rows, schema = probe(binding)
    if not schema_contains_column(schema, binding.result_column):
        raise QueryError(
            f"result_column '{binding.result_column}' was not returned for binding {binding.binding_id}."
        )
    if not column_is_numeric(schema, binding.result_column, rows):
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