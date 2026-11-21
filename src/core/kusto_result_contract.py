from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any

from src.core.exceptions import QueryError
from src.core.integration_types import (
    KustoResultColumn,
    KustoResultKind,
    KustoResultSchema,
    KustoResultSet,
)
from src.core.kusto_result_limits import KustoResultLimits, admit_kusto_rows

_TEMPORAL_TYPE_NAMES = frozenset({"date", "datetime", "timestamp"})
_NUMERIC_TYPE_NAMES = frozenset({
    "decimal",
    "double",
    "float",
    "int",
    "int64",
    "integer",
    "long",
    "real",
})


def build_kusto_result_schema(
    rows: Sequence[Mapping[str, Any]],
    *,
    columns: tuple[KustoResultColumn, ...] = (),
    expected_cardinality: str = "zero_ok",
    strict_rows: bool = True,
) -> KustoResultSchema:
    resolved_columns = columns or _infer_columns(rows)
    names = tuple(column.name for column in resolved_columns)
    if len(names) != len(set(names)):
        raise QueryError("Kusto result schema contains duplicate column names.")
    if strict_rows:
        for row in rows:
            if tuple(row) != names:
                raise QueryError("Kusto result rows do not match the admitted schema.")

    if expected_cardinality == "scalar_required":
        kind = KustoResultKind.SCALAR
    else:
        kind = KustoResultKind.TABLE
    time_column = next(
        (
            column.name
            for column in resolved_columns
            if _is_temporal_column(column)
        ),
        None,
    )
    value_columns = tuple(
        column.name
        for column in resolved_columns
        if column.name != time_column and _is_numeric_column(column)
    )
    if expected_cardinality != "scalar_required" and time_column and value_columns:
        kind = KustoResultKind.TIME_SERIES
    if kind is not KustoResultKind.TIME_SERIES:
        time_column = None
        value_columns = ()
    return KustoResultSchema(
        kind=kind,
        columns=resolved_columns,
        row_count=len(rows),
        time_column=time_column,
        value_columns=value_columns,
    )


def admit_kusto_execution(
    execution: list[dict[str, Any]] | tuple[dict[str, Any], ...] | KustoResultSet,
    *,
    query_id: str,
    expected_cardinality: str,
    result_limits: KustoResultLimits | None,
) -> tuple[list[dict[str, Any]], KustoResultSchema]:
    supplied_schema: KustoResultSchema | None = None
    if isinstance(execution, KustoResultSet):
        if execution.query_id != query_id:
            raise QueryError("Kusto typed result query identity does not match the admitted query.")
        if execution.is_partial:
            raise QueryError("Kusto typed result is partial and cannot be admitted.")
        if execution.row_count is not None and execution.row_count != len(execution.rows):
            raise QueryError("Kusto typed result row count does not match its admitted rows.")
        raw_rows: Sequence[Mapping[str, Any]] = execution.rows
        supplied_schema = execution.schema
    elif isinstance(execution, (list, tuple)):
        raw_rows = execution
    else:
        raise QueryError("Kusto executor returned an unsupported result type.")

    rows = admit_kusto_rows(raw_rows, result_limits)
    if expected_cardinality == "scalar_required" and len(rows) != 1:
        raise QueryError(
            f"Kusto query {query_id} expected scalar_required (exactly 1 row); "
            f"actual {len(rows)} row(s)."
        )
    if supplied_schema is not None:
        validate_kusto_result_schema(rows, supplied_schema)
        if expected_cardinality == "scalar_required" and supplied_schema.kind is not KustoResultKind.SCALAR:
            raise QueryError("Kusto scalar_required result requires a scalar typed schema.")
        return [dict(row) for row in rows], supplied_schema
    return [dict(row) for row in rows], build_kusto_result_schema(
        rows,
        expected_cardinality=expected_cardinality,
        strict_rows=False,
    )


def validate_kusto_result_schema(
    rows: Sequence[Mapping[str, Any]],
    schema: KustoResultSchema,
) -> None:
    names = tuple(column.name for column in schema.columns)
    if len(names) != len(set(names)) or any(not name.strip() for name in names):
        raise QueryError("Kusto result schema contains invalid or duplicate column names.")
    if schema.row_count != len(rows):
        raise QueryError("Kusto typed result schema row count does not match its rows.")
    if any(tuple(row) != names for row in rows):
        raise QueryError("Kusto result rows do not match the admitted schema.")

    if schema.kind is KustoResultKind.SCALAR:
        if len(rows) != 1 or schema.time_column is not None or schema.value_columns:
            raise QueryError("Kusto scalar result schema must contain exactly one row and no series axes.")
        return

    if schema.kind is KustoResultKind.TABLE:
        if schema.time_column is not None or schema.value_columns:
            raise QueryError("Kusto typed result schema kind does not match its declared axes.")
        return

    if schema.kind is KustoResultKind.TIME_SERIES:
        if schema.time_column not in names:
            raise QueryError("Kusto typed result schema time column is invalid.")
        if (
            not schema.value_columns
            or any(column not in names for column in schema.value_columns)
            or schema.time_column in schema.value_columns
        ):
            raise QueryError("Kusto typed result schema value columns are invalid.")
        return

    raise QueryError("Kusto result schema has an unsupported result kind.")


def _infer_columns(rows: Sequence[Mapping[str, Any]]) -> tuple[KustoResultColumn, ...]:
    if not rows:
        return ()
    return tuple(
        KustoResultColumn(name, _infer_type_name(tuple(row.get(name) for row in rows)))
        for name in rows[0]
    )


def _infer_type_name(values: tuple[Any, ...]) -> str | None:
    non_null = tuple(value for value in values if value is not None)
    if not non_null:
        return None
    if all(isinstance(value, bool) for value in non_null):
        return "bool"
    if all(_is_temporal_value(value) for value in non_null):
        return "datetime"
    if all(isinstance(value, int) and not isinstance(value, bool) for value in non_null):
        return "long"
    if all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in non_null):
        return "real"
    return "string"


def _is_temporal_value(value: Any) -> bool:
    if isinstance(value, datetime | date):
        return True
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value)
        return True
    except (ValueError, OverflowError):
        return False


def _is_temporal_column(column: KustoResultColumn) -> bool:
    return (column.type_name or "").lower() in _TEMPORAL_TYPE_NAMES


def _is_numeric_column(column: KustoResultColumn) -> bool:
    return (column.type_name or "").lower() in _NUMERIC_TYPE_NAMES


__all__ = [
    "admit_kusto_execution",
    "build_kusto_result_schema",
    "validate_kusto_result_schema",
]
